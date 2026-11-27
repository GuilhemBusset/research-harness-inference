"""The private mirror of the images the pipeline's builds pull (src/rhi/pipeline/mirror.py; system
design, section 8.2): five ECR repositories of the pipeline stack, named rhi-mirror/ and the image's
path on Docker Hub, each retained; the action Mirror before Synth in the stage Build, which runs
the mirror's script on the source with crane checked against its checksum; and a role that may
get an ECR authorization token and pull, push and describe images under rhi-mirror/ of the
deploying account and region, beside what CDK Pipelines gives every project, and nothing else.

The tests read the app's own pipeline, the one that is deployed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

Templates = dict[str, dict[str, Any]]

PIPELINE_TEMPLATE = "RhiPipeline.template.json"
REPOSITORIES = {
    "rhi-mirror/library/python",
    "rhi-mirror/grafana/grafana-oss",
    "rhi-mirror/vllm/vllm-openai",
    "rhi-mirror/tonistiigi/binfmt",
    "rhi-mirror/moby/buildkit",
}
SCRIPT = "scripts/pipeline/mirror_images.py"
CRANE_URL = (
    "https://github.com/google/go-containerregistry/releases/download/v0.22.1/go-containerregistry_Linux_x86_64.tar.gz"
)
CRANE_SHA256 = "0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0"
PULL_PUSH_DESCRIBE = {
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ecr:BatchCheckLayerAvailability",
    "ecr:InitiateLayerUpload",
    "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload",
    "ecr:PutImage",
    "ecr:DescribeImages",
}
# Partition, region and account of the deploying environment, each in its own field.
IN_THE_DEPLOYING_ACCOUNT = "arn:<AWS::Partition>:{service}:<AWS::Region>:<AWS::AccountId>"
UNDER_THE_PREFIX = IN_THE_DEPLOYING_ACCOUNT.format(service="ecr") + ":repository/rhi-mirror/*"


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def text(value: Any) -> str:
    """A value of the template as text, `<Name>` in place of each pseudo parameter or resource it
    refers to, and `<Name.Attribute>` in place of each attribute."""
    if isinstance(value, str):
        return value
    ((function, argument),) = value.items()
    if function == "Fn::Join":
        return argument[0].join(text(part) for part in argument[1])
    if function == "Ref":
        return f"<{argument}>"
    assert function == "Fn::GetAtt", value
    return f"<{'.'.join(argument)}>"


class Mirror:
    """The step of the synthesized pipeline: the actions of its stage, its project and what its
    role is allowed, as `(action, resource)` pairs."""

    def __init__(self, templates: Templates) -> None:
        self.resources = templates[PIPELINE_TEMPLATE]["Resources"]
        (pipeline,) = [r["Properties"] for r in self.resources.values() if r["Type"] == "AWS::CodePipeline::Pipeline"]
        self.pipeline = pipeline
        (self.stage,) = [stage for stage in pipeline["Stages"] if stage["Name"] == "Build"]
        (self.action,) = [action for action in self.stage["Actions"] if action["Name"] == "Mirror"]
        project_id = self.action["Configuration"]["ProjectName"]["Ref"]
        self.project = self.resources[project_id]["Properties"]
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


@pytest.fixture(scope="module")
def mirror(templates: Templates) -> Mirror:
    return Mirror(templates)


def test_the_pipeline_stack_holds_the_five_repositories_each_retained(templates: Templates) -> None:
    repositories = [
        r for r in templates[PIPELINE_TEMPLATE]["Resources"].values() if r["Type"] == "AWS::ECR::Repository"
    ]
    assert sorted(r["Properties"]["RepositoryName"] for r in repositories) == sorted(REPOSITORIES)
    assert all(r["DeletionPolicy"] == r["UpdateReplacePolicy"] == "Retain" for r in repositories)


def test_mirror_runs_before_synth_in_the_stage_build_on_the_source(mirror: Mirror) -> None:
    order = {action["Name"]: action["RunOrder"] for action in mirror.stage["Actions"]}
    assert order["Mirror"] < order["Synth"]
    assert mirror.action["ActionTypeId"]["Provider"] == "CodeBuild"
    (source,) = mirror.pipeline["Stages"][0]["Actions"]
    assert mirror.action["InputArtifacts"] == source["OutputArtifacts"]
    phases = json.loads(mirror.project["Source"]["BuildSpec"])["phases"]
    # crane, pinned and checked against its checksum before it is unpacked.
    install = phases["install"]["commands"]
    (download,) = [at for at, command in enumerate(install) if CRANE_URL in command]
    (check,) = [at for at, command in enumerate(install) if CRANE_SHA256 in command and "sha256sum -c" in command]
    (unpack,) = [at for at, command in enumerate(install) if command.startswith("tar ")]
    assert download < check < unpack
    assert phases["build"]["commands"] == ["uv sync --locked", f"uv run --locked python {SCRIPT}"]


def test_the_mirror_s_role_reaches_the_token_and_the_repositories_under_rhi_mirror_alone(mirror: Mirror) -> None:
    every_project = {
        *((action, mirror.artifacts) for action in ("s3:GetBucket*", "s3:GetObject*", "s3:List*")),
        *((action, f"{mirror.artifacts}/*") for action in ("s3:GetBucket*", "s3:GetObject*", "s3:List*")),
        *(
            (action, IN_THE_DEPLOYING_ACCOUNT.format(service="logs") + f":log-group:/aws/codebuild/{mirror.own}{suffix}")
            for action in ("logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents")
            for suffix in ("", ":*")
        ),
        *(
            (action, IN_THE_DEPLOYING_ACCOUNT.format(service="codebuild") + f":report-group/{mirror.own}-*")
            for action in (
                "codebuild:CreateReportGroup",
                "codebuild:CreateReport",
                "codebuild:UpdateReport",
                "codebuild:BatchPutTestCases",
                "codebuild:BatchPutCodeCoverages",
            )
        ),
    }
    own = mirror.allowed - every_project
    assert own == {("ecr:GetAuthorizationToken", "*"), *((action, UNDER_THE_PREFIX) for action in PULL_PUSH_DESCRIBE)}
