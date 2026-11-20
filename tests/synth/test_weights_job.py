"""The pipeline's step that copies the weights (src/rhi/pipeline/weights.py; system design,
sections 4.4 and 8.2): the action named CopyWeights, before the stage's deployment, which runs the
copy job on the source; the model bucket's name in its environment, the name the stage gives the
bucket; and a role that lists the bucket, puts and deletes its objects, reads the configuration's
path, and holds nothing else but what CDK Pipelines gives every project.

The tests read the app's own pipeline, the one that is deployed. Its configuration is the
example's, which has no GPU pool: the step is there whatever the pools.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

Templates = dict[str, dict[str, Any]]

REPO = Path(__file__).resolve().parents[2]
PIPELINE_TEMPLATE = "RhiPipeline.template.json"
STAGE = "Rhi"
ACTION = "CopyWeights"
SCRIPT = "scripts/pipeline/copy_weights.py"
# The model bucket of the deploying account and region, in a policy and by its name.
MODELS = "arn:<>:s3:::rhi-models-<>-<>"
MODELS_NAME = "rhi-models-<>-<>"
CONFIGURATION = "arn:<>:ssm:<>:<>:parameter/rhi/config"


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


class Step:
    """The step of a synthesized pipeline: its action, the actions of its stage, its project and
    what its role is allowed, as `(action, resource)` pairs."""

    def __init__(self, templates: Templates) -> None:
        self.resources = templates[PIPELINE_TEMPLATE]["Resources"]
        (pipeline,) = [r["Properties"] for r in self.resources.values() if r["Type"] == "AWS::CodePipeline::Pipeline"]
        self.pipeline = pipeline
        # One action of that name in the whole pipeline, in the stage whose weights it copies.
        ((stage, self.action),) = [
            (stage, action) for stage in pipeline["Stages"] for action in stage["Actions"] if action["Name"] == ACTION
        ]
        assert stage["Name"] == STAGE
        self.actions = stage["Actions"]
        self.project_id = self.action["Configuration"]["ProjectName"]["Ref"]
        self.project = self.resources[self.project_id]["Properties"]
        self.phases = json.loads(self.project["Source"]["BuildSpec"])["phases"]
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

    def run_orders(self, provider: str) -> list[int]:
        return [action["RunOrder"] for action in self.actions if action["ActionTypeId"]["Provider"] == provider]


@pytest.fixture(scope="module")
def step(templates: Templates) -> Step:
    """The step of the app's pipeline."""
    return Step(templates)


def test_the_step_comes_before_the_stage_s_deployment(step: Step) -> None:
    assert step.action["ActionTypeId"] == {"Category": "Build", "Owner": "AWS", "Provider": "CodeBuild", "Version": "1"}
    deployed_at = step.run_orders("CloudFormation")
    assert deployed_at and step.action["RunOrder"] < min(deployed_at)
    # And after the approval on widened permissions: the weights of a release that will be deployed.
    approved_at = step.run_orders("Manual")
    assert approved_at and step.action["RunOrder"] > max(approved_at)
    # It runs on the source: the manifests of the commit the execution deploys.
    (source,) = step.pipeline["Stages"][0]["Actions"]
    assert step.action["InputArtifacts"] == source["OutputArtifacts"]


def test_the_step_runs_the_copy_job_in_a_locked_environment(step: Step) -> None:
    install, build = step.phases["install"]["commands"], step.phases["build"]["commands"]
    assert all(re.fullmatch(r"python3 -m pip install uv==\d+\.\d+\.\d+", command) for command in install) and install
    # The job is a command of its own, after the locked environment: its exit is the build's.
    assert build == ["uv sync --locked", f"uv run --locked python {SCRIPT}"]
    assert (REPO / SCRIPT).is_file()
    assert set(step.phases) == {"install", "build"}
    assert step.project["Environment"]["PrivilegedMode"] is False


def test_the_job_is_told_the_stage_s_model_bucket_and_given_the_import_path(step: Step, templates: Templates) -> None:
    given = {entry["Name"]: entry["Value"] for entry in step.project["Environment"]["EnvironmentVariables"]}
    assert text(given["RHI_MODEL_BUCKET"]) == MODELS_NAME
    # It is the name the stage gives its model bucket, with the account and region of the deployment.
    named = [
        resource["Properties"].get("BucketName")
        for path, template in templates.items()
        if path != PIPELINE_TEMPLATE
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::S3::Bucket" and "BucketName" in resource["Properties"]
    ]
    assert given["RHI_MODEL_BUCKET"] in named
    # The job imports the configuration's loader, and the pool's fields with it.
    assert {"src", "services/common"} <= set(given["PYTHONPATH"].split(":"))


def test_the_role_lists_writes_and_deletes_in_the_model_bucket_reads_the_configuration_and_nothing_else(
    step: Step,
) -> None:
    its_own = {
        ("s3:ListBucket", MODELS),
        ("s3:PutObject", f"{MODELS}/*"),
        ("s3:DeleteObject", f"{MODELS}/*"),
        ("ssm:GetParametersByPath", CONFIGURATION),
        ("ssm:GetParametersByPath", f"{CONFIGURATION}/*"),
    }
    assert its_own <= step.allowed
    # Beside that, what CDK Pipelines gives every project: its own logs, its own test reports and a
    # read of its input in the pipeline's artifact bucket.
    logs = f"arn:<>:logs:<>:<>:log-group:/aws/codebuild/<{step.project_id}>"
    reports = f"arn:<>:codebuild:<>:<>:report-group/<{step.project_id}>-*"
    (artifacts,) = [
        logical_id for logical_id, resource in step.resources.items() if resource["Type"] == "AWS::S3::Bucket"
    ]
    of_every_project = {
        *(
            (action, resource)
            for action in ("s3:GetBucket*", "s3:GetObject*", "s3:List*")
            for resource in (f"<{artifacts}.Arn>", f"<{artifacts}.Arn>/*")
        ),
        *(
            (f"logs:{action}", resource)
            for action in ("CreateLogGroup", "CreateLogStream", "PutLogEvents")
            for resource in (logs, f"{logs}:*")
        ),
        *(
            (f"codebuild:{action}", reports)
            for action in (
                "BatchPutCodeCoverages",
                "BatchPutTestCases",
                "CreateReport",
                "CreateReportGroup",
                "UpdateReport",
            )
        ),
    }
    assert step.allowed - its_own == of_every_project
    # So no statement reads an object of the model bucket.
    assert not any(action.startswith("s3:Get") and resource.startswith(MODELS) for action, resource in step.allowed)
