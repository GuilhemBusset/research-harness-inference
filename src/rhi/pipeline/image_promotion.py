"""The job that promotes a GPU pool's machine image after a canary start on it passes (system
design, sections 4.3 and 5.1).

The job is a CodeBuild project of its own, `PROJECT_NAME`, beside the pipeline and not in it: the
owner starts a build of it by hand, giving the pool, the candidate image's ID and the instance
families the candidate lists as environment variables (`scripts/pipeline/README.md`). Nothing else
starts it: it is no action of the pipeline, it has no webhook, and no rule or schedule targets it.
`tests/policy/test_pipeline.py` holds it to that.

**What it runs.** `scripts/pipeline/promote_image.py`, from `main` of this repository, cloned over
HTTPS when the build starts: the repository is public, and the clone needs no credential. The
pipeline's name reaches the script in `RHI_PROMOTE_PIPELINE`: after it writes a pool's image
parameter, it starts an execution of that pipeline, whose deployment takes the image and refreshes
the pool.

**The role** is the one role of the app that may write a pool's image parameter or start an
instance, and holds nothing more than the job needs:

- `ec2:RunInstances` only from a launch template tagged as a GPU pool's: one that carries the tag
  `rhi-engine` with a value other than `mock` (`rhi.pools.group`). Each of the other resources
  of the call (the instance, its volume and network interface, the subnet, the security group, the
  image) is admitted only in a call that names a launch template;
- `ec2:CreateTags` on the instance and its volume only in that call (`ec2:CreateAction` is
  `RunInstances`): EC2 asks for it to apply the tags of the call and of the launch template;
- `iam:PassRole` on the roles under the GPU pools' IAM path, `/rhi/gpu-pools/` (`rhi.pools.role`),
  to EC2 only: the launch template's instance profile holds the pool's role;
- `ec2:GetConsoleOutput` and `ec2:TerminateInstances` on the instances that carry the job's tag
  only;
- `ssm:PutParameter` on the parameters under `/rhi/images/` (`rhi.pools.image`);
- `codepipeline:StartPipelineExecution` on this project's pipeline only;
- `autoscaling:DescribeAutoScalingGroups`, `ec2:DescribeLaunchTemplateVersions`,
  `ec2:DescribeImages` and `ec2:DescribeInstances`, on every resource: AWS grants them no other
  way. The acknowledgement file, `tests/policy/acknowledged/pipeline_image_promotion.toml`, lists
  each with its reason;
- and what CodeBuild gives a project: its own log group and report groups.

Nothing of Auto Scaling but the description: the scaler stays the only writer of a pool's size.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_iam as iam
from constructs import Construct

from rhi.pipeline.synth import UV_VERSION
from rhi.pools import group, image, role

if TYPE_CHECKING:
    from aws_cdk import aws_codepipeline as codepipeline

    from rhi.config import Config

# The project's fixed name: the owner starts a build of it by this name.
PROJECT_NAME = "rhi-promote-image"
SCRIPT = "scripts/pipeline/promote_image.py"
# The branch the job runs from: the one the pipeline deploys.
BRANCH = "main"
# The variable that names the pipeline to the script, and those the owner gives each build.
PIPELINE_VARIABLE = "RHI_PROMOTE_PIPELINE"
POOL_VARIABLE = "RHI_PROMOTE_POOL"
IMAGE_VARIABLE = "RHI_PROMOTE_IMAGE"
FAMILIES_VARIABLE = "RHI_PROMOTE_FAMILIES"
# The tag the script gives its canary, which the role's reads and terminations require
# (`promote_image.JOB_TAG`).
JOB_TAG = "rhi-promotion"
JOB_TAG_VALUE = "canary"
# The engine of the pool whose launch template the job may not start from.
MOCK_ENGINE = image.MOCK_ENGINE

# Longer than the script's own time limit for the canary, with the clone and the install.
TIMEOUT = cdk.Duration.minutes(60)

# What each describe action is for, which AWS grants only on every resource.
DESCRIBES = [
    "autoscaling:DescribeAutoScalingGroups",
    "ec2:DescribeLaunchTemplateVersions",
    "ec2:DescribeImages",
    "ec2:DescribeInstances",
]


def commands(config: Config) -> list[str]:
    """The build's commands: clone the repository at `BRANCH`, then run the script on the owner's
    variables."""
    repository = f"https://github.com/{config.github_owner}/{config.github_repo}.git"
    return [
        f"git clone --depth 1 --branch {BRANCH} {repository} repository",
        (
            f"cd repository && uv run --locked python {SCRIPT}"
            f' --pool "${POOL_VARIABLE}" --image "${IMAGE_VARIABLE}" --families "${FAMILIES_VARIABLE}"'
        ),
    ]


class ImagePromotion(Construct):
    """The promotion job, as `project`, and its role, as `role`, for the pipeline `pipeline`."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config, pipeline: codepipeline.Pipeline) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        codebuild_service = iam.ServicePrincipal("codebuild.amazonaws.com")
        # ty reads the CDK's generated principal and role as unlike their protocols: jsii passes a
        # renamed parameter by position.
        self.role = iam.Role(self, "Role", assumed_by=codebuild_service)  # ty: ignore[invalid-argument-type]
        for statement in statements(stack, pipeline):
            self.role.add_to_policy(statement)
        self.project = codebuild.Project(
            self,
            "Project",
            project_name=PROJECT_NAME,
            role=self.role,  # ty: ignore[invalid-argument-type] - as above
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "install": {"commands": [f"python3 -m pip install uv=={UV_VERSION}"]},
                        "build": {"commands": commands(config)},
                    },
                }
            ),
            environment=codebuild.BuildEnvironment(build_image=codebuild.LinuxBuildImage.STANDARD_7_0),
            environment_variables={PIPELINE_VARIABLE: codebuild.BuildEnvironmentVariable(value=pipeline.pipeline_name)},
            timeout=TIMEOUT,
        )


def statements(stack: cdk.Stack, pipeline: codepipeline.Pipeline) -> list[iam.PolicyStatement]:
    """What the job's role is allowed, in the account and region of `stack`."""

    def ec2(resource: str, *, account: str = stack.account) -> str:
        return f"arn:{stack.partition}:ec2:{stack.region}:{account}:{resource}"

    templates = ec2("launch-template/*")
    instances = ec2("instance/*")
    volumes = ec2("volume/*")
    gpu_roles = f"arn:{stack.partition}:iam::{stack.account}:role{role.GPU_ROLE_PATH}*"
    images = f"arn:{stack.partition}:ssm:{stack.region}:{stack.account}:parameter{image.IMAGE_PARAMETER_PREFIX}*"
    engine = f"aws:ResourceTag/{group.ENGINE_TAG}"
    return [
        # The launch template: a GPU pool's only, by its engine's tag.
        iam.PolicyStatement(
            actions=["ec2:RunInstances"],
            resources=[templates],
            conditions={"Null": {engine: "false"}, "StringNotEquals": {engine: MOCK_ENGINE}},
        ),
        # The other resources of the call, in a call that names a launch template.
        iam.PolicyStatement(
            actions=["ec2:RunInstances"],
            resources=[
                instances,
                volumes,
                ec2("network-interface/*"),
                ec2("subnet/*"),
                ec2("security-group/*"),
                ec2("image/*", account=""),
            ],
            conditions={"ArnLike": {"ec2:LaunchTemplate": templates}},
        ),
        # The tags of the call and of the launch template, on what the call creates, in the call.
        iam.PolicyStatement(
            actions=["ec2:CreateTags"],
            resources=[instances, volumes],
            conditions={"StringEquals": {"ec2:CreateAction": "RunInstances"}},
        ),
        # The pool's instance role, which the launch template's instance profile holds.
        iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[gpu_roles],
            conditions={"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}},
        ),
        # The canary's line, and its end: on the job's own instances only.
        iam.PolicyStatement(
            actions=["ec2:GetConsoleOutput", "ec2:TerminateInstances"],
            resources=[instances],
            conditions={"StringEquals": {f"aws:ResourceTag/{JOB_TAG}": JOB_TAG_VALUE}},
        ),
        iam.PolicyStatement(actions=["ssm:PutParameter"], resources=[images]),
        iam.PolicyStatement(actions=["codepipeline:StartPipelineExecution"], resources=[pipeline.pipeline_arn]),
        iam.PolicyStatement(actions=DESCRIBES, resources=["*"]),
    ]
