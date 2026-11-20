"""The pipeline stack: CDK Pipelines on CodePipeline V2, in queued execution, from `main` (system
design, section 8.2).

The only thing that deploys the endpoint is this pipeline (requirement R6). It has six steps:

- its source is this repository, on `main`, through a CodeConnections connection the stack
  creates. The connection starts pending until the owner authorizes it once (section 8.6).
- the synth step of `rhi.pipeline.synth`: it reads the configuration from SSM into a file outside
  the source tree, checks that no private value of it is in the tree, synthesizes from that file
  and runs the tests.
- the self-update, which CDK Pipelines adds.
- the stage, `EndpointStage`, whose deployment waits for the approval CDK Pipelines asks for
  when a change widens permissions. That approval is a tripwire for honest mistakes. It does not
  cover this stack, which updates earlier in the same run (section 8.4, control 6).
- the copy of the weights of `rhi.pipeline.weights`, after that approval and before the stage's
  deployment: each GPU pool's model, at its pinned revision, copied into the model bucket, so that
  a release finds its weights when its instances start. A copy that fails fails the execution
  before anything is deployed.
- the verification of `rhi.pipeline.verify`, after the stage's deployment: the smoke tests against
  the deployed endpoint, with the canary's key. A failed test fails the execution. In its
  post-build phase the step writes its marks to this pipeline's artifact bucket, under
  `verification/<execution ID>/`: `not-run`, its reason when it ran no test,
  `failed-smoke-tests`, the IDs of the tests that failed, and `last-stages`, when its tests
  failed, the last lifecycle stage each instance of the pools it tested reached, which it reads
  from their heartbeat rows. The reporter reads them there.

The asset step builds every image asset of the stage for its pool's processor architecture
(sections 4.2 and 4.3): linux/arm64 for the mock pool, linux/amd64 for the GPU pools. CodeBuild's
build hosts are x86_64, so each project that publishes an image first registers an emulator for
arm64 and selects a builder that can target either platform, all pinned (`IMAGE_BUILDER`).

Beside the pipeline, the reporter of `rhi.pipeline.reporter`: a rule on the pipeline's state and a
function that posts commit statuses to GitHub (step 7), and sends the owner, on the topic of
`rhi.pipeline.notify`, what a public status may not carry of a failure. With it come the
measurement bus and a rule on it that invokes the same function, through which a spike's or the
canary's job hands its numbers to the reporter as events (sections 8.4 and 12). It adds no step to
the pipeline.

A push to `main` is the only trigger. No CodeBuild project has a webhook: on a public repository
it would run a stranger's code with a role of the account (section 8.3). Every role of this stack
carries the permissions boundary of the bounded bootstrap, by its fixed name, as every role of the
stage does.

The stack is environment-agnostic, like the stage: the account and the region come from the
credentials that deploy it (section 10).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codeconnections as codeconnections
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import pipelines

from rhi.pipeline.notify import OwnerTopic
from rhi.pipeline.reporter import Reporter
from rhi.pipeline.synth import synth_step
from rhi.pipeline.verify import verify_step
from rhi.pipeline.weights import copy_weights_step
from rhi.stage import PERMISSIONS_BOUNDARY

if TYPE_CHECKING:
    from constructs import Construct, IConstruct

    from rhi.config import Config

# The only branch that starts the pipeline.
BRANCH = "main"
# The connection's fixed name. The owner finds it under this name to authorize it.
CONNECTION_NAME = "rhi-github"
# The name of the source's action in the pipeline.
SOURCE_ACTION = "GitHub"

# What a project that publishes an image installs, so that one run builds an image for linux/arm64
# as well as one for linux/amd64. Each is pinned, by digest or by version and checksum, and each
# release had been out a week when it was pinned (the cooling-off of sections 9 and 17).
# The emulator: QEMU's arm64 handler, registered with the host's kernel by this image.
EMULATOR_IMAGE = (
    "tonistiigi/binfmt:qemu-v10.2.3@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
)
# The builder: Docker's buildx plugin, for the x86_64 build host, checked against its release's checksum.
BUILDX_VERSION = "0.38.2"
BUILDX_SHA256 = "7992ec09413b80ff57ba4e081f56fc833d7bac1d5e71143c6c809e7e6065f6d6"
BUILDX_URL = (
    f"https://github.com/docker/buildx/releases/download/v{BUILDX_VERSION}/buildx-v{BUILDX_VERSION}.linux-amd64"
)
BUILDX_PLUGIN = "$HOME/.docker/cli-plugins/docker-buildx"
# The BuildKit the builder runs in a container. It builds for any platform the emulator adds, and
# loads what it builds into Docker, from where `cdk-assets` tags and pushes it.
BUILDKIT_IMAGE = "moby/buildkit:v0.34.2@sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe"
BUILDER = "rhi-assets"
IMAGE_BUILDER = [
    f"docker run --privileged --rm {EMULATOR_IMAGE} --install arm64",
    f'curl -fsSL --create-dirs -o "{BUILDX_PLUGIN}" {BUILDX_URL}',
    f'echo "{BUILDX_SHA256}  {BUILDX_PLUGIN}" | sha256sum -c -',
    f'chmod +x "{BUILDX_PLUGIN}"',
    (
        f"docker buildx create --name {BUILDER} --driver docker-container --driver-opt image={BUILDKIT_IMAGE}"
        " --driver-opt default-load=true --bootstrap --use"
    ),
]
# `docker build`, as `cdk-assets` runs it, is the builder's: BuildKit, on the builder created above.
IMAGE_BUILDER_ENVIRONMENT = {"DOCKER_BUILDKIT": "1", "BUILDX_BUILDER": BUILDER}


class PipelineStack(cdk.Stack):
    """The pipeline, which deploys `stage` and nothing else. `stage` must not be deployed any
    other way."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config, stage: cdk.Stage) -> None:
        super().__init__(
            scope, construct_id, permissions_boundary=cdk.PermissionsBoundary.from_name(PERMISSIONS_BOUNDARY)
        )
        connection = codeconnections.CfnConnection(
            self, "Connection", connection_name=CONNECTION_NAME, provider_type="GitHub"
        )
        # A fixed action name: otherwise CDK Pipelines names the action, and the logical IDs below
        # it, after the repository, which the configuration gives.
        source = pipelines.CodePipelineSource.connection(
            f"{config.github_owner}/{config.github_repo}",
            BRANCH,
            connection_arn=connection.attr_connection_arn,
            action_name=SOURCE_ACTION,
        )
        synth = synth_step(self, source)
        underlying = codepipeline.Pipeline(
            self,
            "CodePipeline",
            pipeline_type=codepipeline.PipelineType.V2,
            # Each commit has its own execution (evidence E20).
            execution_mode=codepipeline.ExecutionMode.QUEUED,
        )
        self.pipeline = pipelines.CodePipeline(self, "Pipeline", code_pipeline=underlying, synth=synth)
        check = pipelines.ConfirmPermissionsBroadening("Check", stage=stage)
        # After every deployment action of the stage: the smoke tests, against what was deployed. The
        # step leaves its marks in the pipeline's artifact bucket, where the reporter reads them. It
        # takes from the stage, as text, what names the stage's heartbeats table to its role: this
        # stack, another assembly, holds no reference to a resource of the stage.
        verify = verify_step(self, source, stage=stage, artifacts=underlying.artifact_bucket)
        # Before any deployment action of the stage, and after the approval: the weights of a
        # release that will be deployed, in the model bucket before an instance asks for them.
        copy_weights = copy_weights_step(self, source)
        copy_weights.add_step_dependency(check)
        self.pipeline.add_stage(stage, pre=[check, copy_weights], post=[verify])
        self.pipeline.build_pipeline()
        self._builds_images_for_both_platforms()

        # CDK grants the source's role the connection's old service prefix only. The source
        # action's documentation asks for both prefixes on the connection, so the new one is added.
        source_scope = underlying.node.find_child("Source").node.find_child(SOURCE_ACTION)
        source_role = source_scope.node.find_child("CodePipelineActionRole")
        assert isinstance(source_role, iam.Role)
        source_role.add_to_principal_policy(
            iam.PolicyStatement(actions=["codeconnections:UseConnection"], resources=[connection.attr_connection_arn])
        )

        # The trigger says what the source already does: a push to `main`, and nothing else.
        (source_action,) = underlying.stages[0].actions
        underlying.add_trigger(
            provider_type=codepipeline.ProviderType.CODE_STAR_SOURCE_CONNECTION,
            git_configuration=codepipeline.GitConfiguration(
                source_action=source_action,
                push_filter=[codepipeline.GitPushFilter(branches_includes=[BRANCH])],
            ),
        )
        # Before the rewrite below, which visits every function of the stack, the reporter's too.
        self.owner_topic = OwnerTopic(self, "OwnerTopic", config=config)
        self.reporter = Reporter(
            self,
            "Reporter",
            config=config,
            pipeline=underlying,
            stage=stage,
            # ty reads the CDK's generated topic as unlike its protocol: jsii passes a renamed parameter by position.
            topic=self.owner_topic.topic,  # ty: ignore[invalid-argument-type]
        )
        self._as_cfn_lint_reads_it(underlying)

    def _builds_images_for_both_platforms(self) -> None:
        """Gives every project of the asset step that publishes an image `IMAGE_BUILDER`, after
        what CDK Pipelines installs and before it publishes.

        CDK Pipelines gives every asset project the same build settings, and runs Docker only in
        a project that publishes an image, which it makes privileged for that: a privileged
        project of the asset step is one that publishes an image. A project that publishes files
        is left as it is.
        """
        assets = self.pipeline.node.try_find_child("Assets")
        projects = [] if assets is None else assets.node.find_all()
        for project in projects:
            if not isinstance(project, codebuild.CfnProject):
                continue
            if not self.resolve(project.environment).get("privilegedMode"):
                continue
            build_spec = json.loads(self.resolve(project.source)["buildSpec"])
            install = build_spec["phases"].setdefault("install", {})
            install["commands"] = [*install.get("commands", []), *IMAGE_BUILDER]
            variables = build_spec.setdefault("env", {}).setdefault("variables", {})
            variables.update(IMAGE_BUILDER_ENVIRONMENT)
            project.add_property_override("Source.BuildSpec", json.dumps(build_spec, indent=2))

    def _as_cfn_lint_reads_it(self, underlying: codepipeline.Pipeline) -> None:
        """Rewrites what CDK Pipelines writes and cfn-lint warns about, with the same meaning.

        - The pipeline and the function that approves a check with no widened permissions each
          name their role, and depend on it as well (W3005): each keeps its dependency on its
          role's policy alone.
        - A deployment action names the bootstrap's execution role with a join of text and pseudo
          parameters (E3703): it is written as the substitution of the same text.
        """
        resources: list[tuple[cdk.CfnResource, iam.IRole | None]] = [(self._resource(underlying), underlying.role)]
        resources += [
            (self._resource(function), function.role)
            for function in self.node.find_all()
            if isinstance(function, lambda_.Function)
        ]
        for resource, role in resources:
            policy = role.node.try_find_child("DefaultPolicy") if role is not None else None
            if policy is not None:
                resource.add_override("DependsOn", [self.get_logical_id(self._resource(policy))])

        pipeline = self._resource(underlying)
        assert isinstance(pipeline, codepipeline.CfnPipeline)
        for at, stage in enumerate(self.resolve(pipeline.stages)):
            for index, action in enumerate(stage["actions"]):
                substitution = _as_substitution(action.get("configuration", {}).get("RoleArn"))
                if substitution is not None:
                    pipeline.add_property_override(f"Stages.{at}.Actions.{index}.Configuration.RoleArn", substitution)

    @staticmethod
    def _resource(construct: IConstruct) -> cdk.CfnResource:
        resource = construct.node.default_child
        assert isinstance(resource, cdk.CfnResource)
        return resource


def _as_substitution(value: Any) -> dict[str, str] | None:
    """A join of text and references with no delimiter, as the `Fn::Sub` of the same text. None
    for anything else."""
    if not isinstance(value, dict) or list(value) != ["Fn::Join"] or value["Fn::Join"][0] != "":
        return None
    text = []
    for part in value["Fn::Join"][1]:
        if isinstance(part, str):
            text.append(part.replace("${", "${!"))
        elif isinstance(part, dict) and list(part) == ["Ref"] and isinstance(part["Ref"], str):
            text.append("${" + part["Ref"] + "}")
        else:
            return None
    return {"Fn::Sub": "".join(text)}
