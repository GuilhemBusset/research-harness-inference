"""The pipeline stack: CDK Pipelines on CodePipeline V2, in queued execution, from `main` (system
design, section 8.2).

The only thing that deploys the endpoint is this pipeline (requirement R6). It has four steps:

- its source is this repository, on `main`, through a CodeConnections connection the stack
  creates. The connection starts pending until the owner authorizes it once (section 8.6).
- a synth step. Its content is issue E1-10's: reading the configuration from SSM, the leak check,
  synthesis and the tests. Until that issue lands, the step names no configuration source. An
  execution started before then fails closed in the configuration loader instead of deploying
  the example configuration.
- the self-update, which CDK Pipelines adds.
- the stage, `EndpointStage`, whose deployment waits for the approval CDK Pipelines asks for
  when a change widens permissions. That approval is a tripwire for honest mistakes. It does not
  cover this stack, which updates earlier in the same run (section 8.4, control 6).

A push to `main` is the only trigger. No CodeBuild project has a webhook: on a public repository
it would run a stranger's code with a role of the account (section 8.3). Every role of this stack
carries the permissions boundary of the bounded bootstrap, by its fixed name, as every role of the
stage does.

The stack is environment-agnostic, like the stage: the account and the region come from the
credentials that deploy it (section 10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aws_cdk as cdk
from aws_cdk import aws_codeconnections as codeconnections
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_lambda as lambda_
from aws_cdk import pipelines

from rhi.stage import PERMISSIONS_BOUNDARY

if TYPE_CHECKING:
    from aws_cdk import aws_iam as iam
    from constructs import Construct, IConstruct

    from rhi.config import Config

# The only branch that starts the pipeline.
BRANCH = "main"
# The connection's fixed name. The owner finds it under this name to authorize it.
CONNECTION_NAME = "rhi-github"
# The name of the source's action in the pipeline.
SOURCE_ACTION = "GitHub"
# The uv the synth step installs: the one CI pins (`.github/workflows/ci.yml`).
UV_VERSION = "0.11.28"


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
        # No configuration source is named here, neither `RHI_CONFIG` nor `RHI_CONFIG_FILE`: the
        # loader fails closed until issue E1-10 gives the step its own.
        synth = pipelines.ShellStep(
            "Synth",
            input=source,
            install_commands=[f"python3 -m pip install uv=={UV_VERSION}", "npm ci --ignore-scripts"],
            commands=["node_modules/.bin/cdk synth --lookups=false"],
        )
        underlying = codepipeline.Pipeline(
            self,
            "CodePipeline",
            pipeline_type=codepipeline.PipelineType.V2,
            # Each commit has its own execution (evidence E20).
            execution_mode=codepipeline.ExecutionMode.QUEUED,
        )
        self.pipeline = pipelines.CodePipeline(self, "Pipeline", code_pipeline=underlying, synth=synth)
        check = pipelines.ConfirmPermissionsBroadening("Check", stage=stage)
        self.pipeline.add_stage(stage, pre=[check])
        self.pipeline.build_pipeline()

        # The trigger says what the source already does: a push to `main`, and nothing else.
        (source_action,) = underlying.stages[0].actions
        underlying.add_trigger(
            provider_type=codepipeline.ProviderType.CODE_STAR_SOURCE_CONNECTION,
            git_configuration=codepipeline.GitConfiguration(
                source_action=source_action,
                push_filter=[codepipeline.GitPushFilter(branches_includes=[BRANCH])],
            ),
        )
        self._as_cfn_lint_reads_it(underlying)

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
