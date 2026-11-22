"""The pipeline's step that holds the stage's deployment while a session is on
(src/rhi/pipeline/hold.py; system design, sections 5.6 and 8.2): the action named Hold, after the
copy of the weights and before the stage's deployment actions, which runs the hold job on the
source with a timeout of 36 hours; the pools stack's name and the pool-state table's logical ID in
its environment, as text; and a role that reads that stack's resources and that table's items and
holds nothing else but what CDK Pipelines gives every project.

The tests read the app's own pipeline, the one that is deployed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import aws_cdk as cdk

Templates = dict[str, dict[str, Any]]

PIPELINE_TEMPLATE = "RhiPipeline.template.json"
STAGE = "Rhi"
ACTION = "Hold"
SCRIPT = "scripts/pipeline/deploy_hold.py"
STACK_VARIABLE = "RHI_HOLD_POOLS_STACK"
LOGICAL_ID_VARIABLE = "RHI_HOLD_TABLE_LOGICAL_ID"


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def text(value: Any) -> str:
    """A value of the template as text, `<>` in place of each pseudo parameter and `<Name>` or
    `<Name.Attribute>` in place of each resource."""
    if isinstance(value, str):
        return value
    ((function, argument),) = value.items()
    if function == "Fn::Join":
        return argument[0].join(text(part) for part in argument[1])
    if function == "Ref":
        return "<>" if argument.startswith("AWS::") else f"<{argument}>"
    assert function == "Fn::GetAtt", value
    return f"<{'.'.join(argument)}>"


class Hold:
    """The step of the synthesized pipeline: its action, the actions of its stage, its project, its
    environment and what its role is allowed, as `(action, resource)` pairs."""

    def __init__(self, templates: Templates) -> None:
        self.resources = templates[PIPELINE_TEMPLATE]["Resources"]
        (pipeline,) = [r["Properties"] for r in self.resources.values() if r["Type"] == "AWS::CodePipeline::Pipeline"]
        self.pipeline = pipeline
        ((stage, self.action),) = [
            (stage, action) for stage in pipeline["Stages"] for action in stage["Actions"] if action["Name"] == ACTION
        ]
        assert stage["Name"] == STAGE
        self.actions = stage["Actions"]
        project_id = self.action["Configuration"]["ProjectName"]["Ref"]
        self.project = self.resources[project_id]["Properties"]
        self.environment = {v["Name"]: v["Value"] for v in self.project["Environment"]["EnvironmentVariables"]}
        role = self.project["ServiceRole"]["Fn::GetAtt"][0]
        assert not self.resources[role]["Properties"].get("ManagedPolicyArns")
        assert not self.resources[role]["Properties"].get("Policies")
        statements = [
            statement
            for policy in self.resources.values()
            if policy["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy")
            and {"Ref": role} in policy["Properties"].get("Roles", [])
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        ]
        assert all(set(statement) == {"Action", "Effect", "Resource"} for statement in statements)
        assert all(statement["Effect"] == "Allow" for statement in statements)
        self.allowed = {
            (action, text(resource))
            for statement in statements
            for action in as_list(statement["Action"])
            for resource in as_list(statement["Resource"])
        }
        self.own = f"<{project_id}>"
        self.artifacts = f"<{pipeline['ArtifactStore']['Location']['Ref']}.Arn>"

    def run_order(self, name: str) -> int:
        (action,) = [action for action in self.actions if action["Name"] == name]
        return action["RunOrder"]

    def deployed_at(self) -> list[int]:
        return [action["RunOrder"] for action in self.actions if action["ActionTypeId"]["Provider"] == "CloudFormation"]


@pytest.fixture(scope="module")
def hold(templates: Templates) -> Hold:
    return Hold(templates)


def test_the_hold_runs_after_the_copy_and_before_the_stage_s_deployment(hold: Hold) -> None:
    assert hold.action["ActionTypeId"] == {"Category": "Build", "Owner": "AWS", "Provider": "CodeBuild", "Version": "1"}
    assert hold.deployed_at()
    assert hold.run_order("CopyWeights") < hold.action["RunOrder"] < min(hold.deployed_at())
    # It runs the hold job on the source, in a locked environment.
    (source,) = hold.pipeline["Stages"][0]["Actions"]
    assert hold.action["InputArtifacts"] == source["OutputArtifacts"]
    phases = json.loads(hold.project["Source"]["BuildSpec"])["phases"]
    assert phases["build"]["commands"] == ["uv sync --locked", f"uv run --locked python {SCRIPT}"]


def test_the_hold_waits_up_to_codebuild_s_limit(hold: Hold) -> None:
    assert hold.project["TimeoutInMinutes"] == 36 * 60


def test_the_hold_is_told_the_pools_stack_and_the_pool_state_table(
    hold: Hold, templates: Templates, app: cdk.App
) -> None:
    stage: Any = app.node.find_child(STAGE)
    table = stage.pools.tables.pool_state.node.default_child
    stack, logical_id = hold.environment[STACK_VARIABLE], hold.environment[LOGICAL_ID_VARIABLE]
    # Text, not a reference: the stage is another assembly.
    assert (stack, logical_id) == (stage.pools.stack_name, stage.pools.resolve(table.logical_id))
    # The stack the stage deploys under that name holds an unnamed table of that logical ID.
    (path,) = {
        action["Configuration"]["TemplatePath"].partition("::")[2]
        for action in hold.actions
        if action["Configuration"].get("StackName") == stack and "TemplatePath" in action["Configuration"]
    }
    resource = templates[path]["Resources"][logical_id]
    assert resource["Type"] == "AWS::DynamoDB::Table" and "TableName" not in resource["Properties"]


def test_the_hold_s_role_reads_the_stack_resource_and_the_table_and_nothing_else(hold: Hold) -> None:
    stack, logical_id = hold.environment[STACK_VARIABLE], hold.environment[LOGICAL_ID_VARIABLE]
    every_project = {
        *((action, hold.artifacts) for action in ("s3:GetBucket*", "s3:GetObject*", "s3:List*")),
        *((action, f"{hold.artifacts}/*") for action in ("s3:GetBucket*", "s3:GetObject*", "s3:List*")),
        *(
            (action, f"arn:<>:logs:<>:<>:log-group:/aws/codebuild/{hold.own}{suffix}")
            for action in ("logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents")
            for suffix in ("", ":*")
        ),
        *(
            (f"codebuild:{action}", f"arn:<>:codebuild:<>:<>:report-group/{hold.own}-*")
            for action in ("BatchPutCodeCoverages", "BatchPutTestCases", "CreateReport", "CreateReportGroup", "UpdateReport")
        ),
    }
    assert hold.allowed - every_project == {
        ("cloudformation:DescribeStackResource", f"arn:<>:cloudformation:<>:<>:stack/{stack}/*"),
        ("dynamodb:GetItem", f"arn:<>:dynamodb:<>:<>:table/{stack}-{logical_id}-*"),
    }
