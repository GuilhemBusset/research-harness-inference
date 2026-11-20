"""The job that promotes a GPU pool's machine image (src/rhi/pipeline/image_promotion.py; system
design, section 4.3): a CodeBuild project of its own beside the pipeline, which nothing but the
owner starts, that runs the promotion script from `main` with the pipeline's name; and its role,
the app's one role that may write an image parameter or start an instance, which passes only the
GPU pools' roles.

The tests read the app's own pipeline template, which holds the job whatever the pools.
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any

import pytest

Templates = dict[str, dict[str, Any]]

PIPELINE_TEMPLATE = "RhiPipeline.template.json"
PROJECT = "rhi-promote-image"
SCRIPT = "scripts/pipeline/promote_image.py"

# Every resource the role names, as `text` writes it, and the conditions it names them under.
TEMPLATES = "arn:<>:ec2:<>:<>:launch-template/*"
INSTANCES = "arn:<>:ec2:<>:<>:instance/*"
VOLUMES = "arn:<>:ec2:<>:<>:volume/*"
FROM_A_TEMPLATE = {"ArnLike": {"ec2:LaunchTemplate": TEMPLATES}}
A_GPU_TEMPLATE = {
    "Null": {"aws:ResourceTag/rhi-engine": "false"},
    "StringNotEquals": {"aws:ResourceTag/rhi-engine": "mock"},
}
IN_THE_START = {"StringEquals": {"ec2:CreateAction": "RunInstances"}}
TO_EC2 = {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}}
THE_JOB_S = {"StringEquals": {"aws:ResourceTag/rhi-promotion": "canary"}}
GPU_ROLES = "arn:<>:iam::<>:role/rhi/gpu-pools/*"


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def text(value: Any) -> str:
    """A value of the template as text, `<>` in place of each pseudo parameter and `<Name>` or
    `<Name.Attribute>` in place of each resource. Anything else as its JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and len(value) == 1:
        ((function, argument),) = value.items()
        if function == "Fn::Join":
            return argument[0].join(text(part) for part in argument[1])
        if function == "Ref":
            return "<>" if argument.startswith("AWS::") else f"<{argument}>"
        if function == "Fn::GetAtt":
            return f"<{'.'.join(argument)}>"
    return json.dumps(value, sort_keys=True)


def condition_text(condition: dict[str, Any]) -> str:
    """A statement's condition as JSON, each of its values as `text` writes it."""
    return json.dumps(
        {test: {key: text(value) for key, value in keys.items()} for test, keys in condition.items()}, sort_keys=True
    )


def allowed_by_role(resources: dict[str, Any]) -> dict[str, set[tuple[str, str, str]]]:
    """Every role of a template, with what it is allowed, as `(action, resource, condition)`."""
    found: dict[str, set[tuple[str, str, str]]] = {}
    for policy in resources.values():
        if policy["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        for role in policy["Properties"].get("Roles", []):
            for statement in as_list(policy["Properties"]["PolicyDocument"]["Statement"]):
                assert statement["Effect"] == "Allow" and "NotAction" not in statement
                condition = condition_text(statement.get("Condition", {}))
                found.setdefault(role["Ref"], set()).update(
                    (action, text(resource), condition)
                    for action in as_list(statement["Action"])
                    for resource in as_list(statement["Resource"])
                )
    return found


@pytest.fixture(scope="module")
def job(templates: Templates) -> dict[str, Any]:
    """The job's project, its logical ID, its role's logical ID and what that role is allowed."""
    resources = templates[PIPELINE_TEMPLATE]["Resources"]
    ((project_id, project),) = [
        (logical_id, resource["Properties"])
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::CodeBuild::Project" and resource["Properties"].get("Name") == PROJECT
    ]
    role = project["ServiceRole"]["Fn::GetAtt"][0]
    assert not resources[role]["Properties"].get("ManagedPolicyArns")
    assert not resources[role]["Properties"].get("Policies")
    return {"id": project_id, "project": project, "role": role, "allowed": allowed_by_role(resources)[role]}


def test_the_job_runs_the_script_from_main_with_the_pipeline_s_name(templates: Templates, job: dict[str, Any]) -> None:
    project = job["project"]
    assert project["Source"]["Type"] == "NO_SOURCE"
    commands = json.loads(project["Source"]["BuildSpec"])["phases"]["build"]["commands"]
    assert commands[0].startswith("git clone --depth 1 --branch main https://github.com/")
    assert f"uv run --locked python {SCRIPT} --pool " in commands[1]
    ((pipeline_id, _),) = [
        (logical_id, resource)
        for logical_id, resource in templates[PIPELINE_TEMPLATE]["Resources"].items()
        if resource["Type"] == "AWS::CodePipeline::Pipeline"
    ]
    variables = {variable["Name"]: variable["Value"] for variable in project["Environment"]["EnvironmentVariables"]}
    assert variables == {"RHI_PROMOTE_PIPELINE": {"Ref": pipeline_id}}
    assert project["TimeoutInMinutes"] >= 45


def test_the_job_has_no_trigger(templates: Templates, job: dict[str, Any]) -> None:
    assert job["project"].get("Triggers", {"Webhook": False}) == {"Webhook": False}
    for path, template in templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            properties = json.dumps(resource.get("Properties", {}))
            if resource["Type"] == "AWS::CodePipeline::Pipeline":
                assert job["id"] not in properties and PROJECT not in properties, path
            if resource["Type"] in ("AWS::Events::Rule", "AWS::Scheduler::Schedule"):
                assert job["id"] not in properties and PROJECT not in properties, (path, logical_id)


def test_the_role_is_the_one_the_job_needs(job: dict[str, Any]) -> None:
    project = f"<{job['id']}>"
    logs = f"arn:<>:logs:<>:<>:log-group:/aws/codebuild/{project}"
    reports = f"arn:<>:codebuild:<>:<>:report-group/{project}-*"
    none = json.dumps({})

    def under(condition: dict[str, Any]) -> str:
        return json.dumps(condition, sort_keys=True)

    starts = [INSTANCES, VOLUMES, "arn:<>:ec2:<>:<>:network-interface/*", "arn:<>:ec2:<>:<>:subnet/*"]
    starts += ["arn:<>:ec2:<>:<>:security-group/*", "arn:<>:ec2:<>::image/*"]
    expected = {
        ("ec2:RunInstances", TEMPLATES, under(A_GPU_TEMPLATE)),
        *(("ec2:RunInstances", resource, under(FROM_A_TEMPLATE)) for resource in starts),
        ("ec2:CreateTags", INSTANCES, under(IN_THE_START)),
        ("ec2:CreateTags", VOLUMES, under(IN_THE_START)),
        ("iam:PassRole", GPU_ROLES, under(TO_EC2)),
        ("ec2:GetConsoleOutput", INSTANCES, under(THE_JOB_S)),
        ("ec2:TerminateInstances", INSTANCES, under(THE_JOB_S)),
        ("ssm:PutParameter", "arn:<>:ssm:<>:<>:parameter/rhi/images/*", none),
        *(
            (action, "*", none)
            for action in (
                "autoscaling:DescribeAutoScalingGroups",
                "ec2:DescribeLaunchTemplateVersions",
                "ec2:DescribeImages",
                "ec2:DescribeInstances",
            )
        ),
        *(
            (action, f"{logs}{suffix}", none)
            for action in ("logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents")
            for suffix in ("", ":*")
        ),
        *(
            (f"codebuild:{action}", reports, none)
            for action in (
                "CreateReportGroup",
                "CreateReport",
                "UpdateReport",
                "BatchPutTestCases",
                "BatchPutCodeCoverages",
            )
        ),
    }
    pipeline_starts = {entry for entry in job["allowed"] if entry[0] == "codepipeline:StartPipelineExecution"}
    assert job["allowed"] - pipeline_starts == expected
    # The pipeline's own ARN, by a reference to it, and nothing else of CodePipeline.
    ((_, pipeline, condition),) = pipeline_starts
    assert pipeline.startswith("arn:<>:codepipeline:<>:<>:<") and condition == none
    # Nothing of Auto Scaling but its description: the scaler alone sets a group's size.
    assert [action for action, _, _ in job["allowed"] if action.startswith("autoscaling:")] == [
        "autoscaling:DescribeAutoScalingGroups"
    ]


def test_the_role_alone_writes_the_images_and_starts_an_instance(templates: Templates, job: dict[str, Any]) -> None:
    def reaches(entries: set[tuple[str, str, str]], wanted: str) -> bool:
        return any(fnmatch.fnmatchcase(wanted.lower(), action.lower()) for action, _, _ in entries)

    for wanted in ("ssm:PutParameter", "ec2:RunInstances", "iam:PassRole"):
        holders = [
            (path, role)
            for path, template in templates.items()
            for role, entries in allowed_by_role(template.get("Resources", {})).items()
            if reaches(entries, wanted)
        ]
        if wanted == "iam:PassRole":
            # Other roles pass roles of their own stacks; the job's passes the GPU pools' only.
            assert {resource for action, resource, _ in job["allowed"] if action == wanted} == {GPU_ROLES}
            continue
        assert holders == [(PIPELINE_TEMPLATE, job["role"])], wanted
