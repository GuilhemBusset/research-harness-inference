"""The pipeline deploys the stage, from `main` only (system design, section 8.2).

CodePipeline V2 in queued execution, so that each commit has its own execution. The source is a
connection the stack creates, on `main`, for the repository the configuration names. The synth
step synthesizes the app (its own tests are in test_pipeline_synth.py), and the self-update follows
it. The stage's stacks are deployed only by the pipeline, after the approval CDK Pipelines asks for
when a change widens permissions.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rhi import config

if TYPE_CHECKING:
    import aws_cdk as cdk

PIPELINE_TEMPLATE = "RhiPipeline.template.json"
STAGE = "Rhi"
SECURITY_DIFF = "cdk diff -a . --security-only --fail $STAGE_PATH/"


def resources_of(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {
        logical_id: resource.get("Properties", {})
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == kind
    }


def the_pipeline(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    (pipeline,) = resources_of(templates[PIPELINE_TEMPLATE], "AWS::CodePipeline::Pipeline").values()
    return pipeline


def actions_of(pipeline: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {stage["Name"]: stage["Actions"] for stage in pipeline["Stages"]}


def provider(action: dict[str, Any]) -> str:
    return action["ActionTypeId"]["Provider"]


def commands_of(template: dict[str, Any], action: dict[str, Any]) -> list[str]:
    """Every command of the build specification of the project a CodeBuild action runs."""
    project = template["Resources"][action["Configuration"]["ProjectName"]["Ref"]]["Properties"]
    spec = project["Source"]["BuildSpec"]
    text = spec if isinstance(spec, str) else "".join(part for part in spec["Fn::Join"][1] if isinstance(part, str))
    return [command for phase in json.loads(text)["phases"].values() for command in phase.get("commands", [])]


def test_the_app_is_the_pipeline_stack_and_its_stage(templates: dict[str, dict[str, Any]], app: cdk.App) -> None:
    # One stack at the top of the app: every other is a stack of the stage.
    assert [path for path in templates if "/" not in path] == [PIPELINE_TEMPLATE]
    deployed = {
        action["Configuration"]["StackName"]
        for action in actions_of(the_pipeline(templates))[STAGE]
        if provider(action) == "CloudFormation"
    }
    stage = app.node.find_child(STAGE)
    stacks = {child.stack_name for child in stage.node.children if hasattr(child, "stack_name")}
    assert stacks and stacks == deployed


def test_the_pipeline_is_v2_and_queued(templates: dict[str, dict[str, Any]]) -> None:
    pipeline = the_pipeline(templates)
    assert pipeline["PipelineType"] == "V2"
    assert pipeline["ExecutionMode"] == "QUEUED"


def test_one_source_on_main_from_the_stacks_connection(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    pipeline = the_pipeline(templates)
    sources = [
        action for stage in pipeline["Stages"] for action in stage["Actions"] if provider(action).startswith("CodeStar")
    ]
    (source,) = sources
    assert [stage["Name"] for stage in pipeline["Stages"]][0] == "Source"
    (connection,) = resources_of(template, "AWS::CodeConnections::Connection")
    example = config.load({"RHI_CONFIG": "example"})
    assert source["Configuration"] == {
        "ConnectionArn": {"Fn::GetAtt": [connection, "ConnectionArn"]},
        "FullRepositoryId": f"{example.github_owner}/{example.github_repo}",
        "BranchName": "main",
    }
    # The only trigger is a push to `main`, and no CodeBuild project has a webhook.
    (trigger,) = pipeline["Triggers"]
    assert trigger["GitConfiguration"]["Push"] == [{"Branches": {"Includes": ["main"]}}]
    assert set(trigger["GitConfiguration"]) == {"Push", "SourceActionName"}
    for path, of_path in templates.items():
        for project in resources_of(of_path, "AWS::CodeBuild::Project").values():
            assert project.get("Triggers", {"Webhook": False}) == {"Webhook": False}, path


def test_the_self_update_follows_the_synth_step(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    actions = actions_of(the_pipeline(templates))
    (synth,) = actions["Build"]
    commands = commands_of(template, synth)
    assert any("cdk synth --lookups=false" in command for command in commands)
    assert [action["Name"] for action in actions["UpdatePipeline"]] == ["SelfMutate"]


def test_the_stage_is_deployed_after_the_approval_on_widened_permissions(templates: dict[str, dict[str, Any]]) -> None:
    template = templates[PIPELINE_TEMPLATE]
    actions = actions_of(the_pipeline(templates))[STAGE]
    checks = [
        action["RunOrder"]
        for action in actions
        if provider(action) == "CodeBuild"
        and any(SECURITY_DIFF in command for command in commands_of(template, action))
    ]
    approvals = [action["RunOrder"] for action in actions if provider(action) == "Manual"]
    deployments = [action["RunOrder"] for action in actions if provider(action) == "CloudFormation"]
    assert checks and approvals and deployments
    assert max(checks) < min(approvals)
    assert max(approvals) < min(deployments)


def test_every_role_of_the_pipeline_carries_the_boundary(templates: dict[str, dict[str, Any]]) -> None:
    roles = resources_of(templates[PIPELINE_TEMPLATE], "AWS::IAM::Role")
    assert roles
    boundary = {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                ":iam::",
                {"Ref": "AWS::AccountId"},
                ":policy/rhi-permissions-boundary",
            ],
        ]
    }
    assert {logical_id: role.get("PermissionsBoundary") for logical_id, role in roles.items()} == dict.fromkeys(
        roles, boundary
    )


def test_the_source_can_use_the_stacks_connection(templates: dict[str, dict[str, Any]]) -> None:
    # The source action's role holds both service prefixes of the permission on the connection.
    template = templates[PIPELINE_TEMPLATE]
    pipeline = the_pipeline(templates)
    (source,) = actions_of(pipeline)["Source"]
    role = source.get("RoleArn", pipeline["RoleArn"])["Fn::GetAtt"][0]
    allowed = set()
    for policy in resources_of(template, "AWS::IAM::Policy").values():
        if {"Ref": role} not in policy["Roles"]:
            continue
        for statement in policy["PolicyDocument"]["Statement"]:
            if statement["Effect"] == "Allow" and statement["Resource"] == source["Configuration"]["ConnectionArn"]:
                actions = statement["Action"]
                allowed |= {actions} if isinstance(actions, str) else set(actions)
    assert {"codeconnections:UseConnection", "codestar-connections:UseConnection"} <= allowed
