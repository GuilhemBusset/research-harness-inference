"""The pipeline's verification step (system design, section 8.2, step 6).

After the stage is deployed, the step runs the smoke tests against the deployed endpoint with the
canary's key: `scripts/pipeline/verify.py`, on the source, in a locked environment. It builds no
image and runs outside the private network. A failed test fails the step, and with it the
execution (section 8.5).

**The action is named `ACTION`.** The reporter maps an action of that name to the context
`verify/<stage>`, so the step's name is its contract: `tests/policy/test_pipeline.py` lists it.

**What the step is told of the stage** comes from outputs of the stage's stacks, through CDK
Pipelines' outputs of a deployed stack. Nothing is looked up, and the pipeline's template holds a
reference to each output, never its value:

- `RHI_SMOKE_ENDPOINT`: the endpoint's address, the gateway stack's output, which reads `none`
  while the endpoint has no HTTPS listener.
- `RHI_SMOKE_WORKSPACE_URL`: the Prometheus workspace's query address, the observability stack's.
- `RHI_VERIFY_RELEASE_PARAMETERS`: each pool's name and the name of its release parameter, the
  pools stack's. The script hands it to the smoke tests as the JSON they read.

and of the execution:

- `RHI_VERIFY_EXECUTION`: the execution's ID, from the pipeline's variable for it;
- `RHI_VERIFY_BUCKET`: the pipeline's artifact bucket;
- `RHI_VERIFY_REASON_FILE`: where the script leaves its reason when it runs no test.

**When no test runs.** While the endpoint has no listener, or the canary's secret is missing or
holds no key, the script runs no test, writes its reason to that file, `listener-off` or
`canary-missing`, and exits 0. The step's post-build phase copies the file, when there is one, to
the object `verification/<execution ID>/not-run` of the artifact bucket, where the reporter reads
that the verification was not run. A copy that fails fails the step: a verification that was not
run is never taken for one that passed.

**The role** may read the canary's secret, `rhi/canary-key`; query the workspace of the stage it
verifies, which it names by the workspace's tag, since the workspace has no ID when the role is
written (`rhi.observability.stack`); read the release parameter of each pool the stage deploys, by
its name; and write under `verification/` of the artifact bucket. Beside that it holds what CDK
Pipelines gives every project of the pipeline: its own logs, its own test reports and a read of
its input in the artifact bucket. Nothing else: `tests/policy/test_pipeline.py` holds it to that.

The step is a step, not a construct: CDK Pipelines creates its project and role inside the
pipeline construct of `rhi.pipeline.stack`, whose acknowledgement file covers them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_iam as iam
from aws_cdk import pipelines

from rhi.gateway.stack import NO_ENDPOINT
from rhi.observability.stack import STAGE_TAG
from rhi.pipeline.synth import UV_VERSION
from rhi.stage import EndpointStage

if TYPE_CHECKING:
    from aws_cdk import aws_s3 as s3

# The name of the step's action, which the reporter reads.
ACTION = "Verify"
SCRIPT = "scripts/pipeline/verify.py"
# The secret the admin CLI's canary command writes the canary's key into (`rhi_admin.canary`).
CANARY_SECRET = "rhi/canary-key"
# Where, in the artifact bucket, the reporter reads what a verification left: under this prefix,
# in the folder of the execution's ID.
PREFIX = "verification"
NOT_RUN = "not-run"
# The file the script leaves its reason in, outside the checkout.
REASON_FILE = "/tmp/rhi-verification-not-run"

ENDPOINT_VARIABLE = "RHI_SMOKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SMOKE_WORKSPACE_URL"
RELEASE_PARAMETERS_VARIABLE = "RHI_VERIFY_RELEASE_PARAMETERS"
EXECUTION_VARIABLE = "RHI_VERIFY_EXECUTION"
BUCKET_VARIABLE = "RHI_VERIFY_BUCKET"
REASON_FILE_VARIABLE = "RHI_VERIFY_REASON_FILE"
# CodePipeline's variable for the ID of the execution an action runs in.
EXECUTION_ID = "#{codepipeline.PipelineExecutionId}"

INSTALL_COMMANDS = [f"python3 -m pip install uv=={UV_VERSION}"]
COMMANDS = ["uv sync --locked", f"uv run --locked python {SCRIPT}"]
# The reason, when the script left one, as the object the reporter reads.
NOT_RUN_OBJECT = f"s3://${BUCKET_VARIABLE}/{PREFIX}/${EXECUTION_VARIABLE}/{NOT_RUN}"
POST_BUILD_COMMANDS = [
    f'if [ -f "${REASON_FILE_VARIABLE}" ]; then aws s3 cp "${REASON_FILE_VARIABLE}" "{NOT_RUN_OBJECT}"; fi'
]


def verify_step(
    scope: cdk.Stack, source: pipelines.IFileSetProducer, *, stage: cdk.Stage, artifacts: s3.IBucket
) -> pipelines.CodeBuildStep:
    """The verification step of the pipeline of `scope`, on `source`, for `stage`, the stage it
    runs after, in a pipeline whose artifact bucket is `artifacts`."""
    environment = {
        EXECUTION_VARIABLE: EXECUTION_ID,
        BUCKET_VARIABLE: artifacts.bucket_name,
        REASON_FILE_VARIABLE: REASON_FILE,
    }
    outputs: dict[str, cdk.CfnOutput] = {}
    # Secrets Manager adds six random characters to a secret's name in its ARN.
    secret = scope.format_arn(
        service="secretsmanager",
        resource="secret",
        resource_name=f"{CANARY_SECRET}-??????",
        arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
    )
    statements = [
        iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"], resources=[secret]),
        iam.PolicyStatement(actions=["s3:PutObject"], resources=[artifacts.arn_for_objects(f"{PREFIX}/*")]),
    ]
    if isinstance(stage, EndpointStage):
        outputs[ENDPOINT_VARIABLE] = stage.gateway.endpoint
        outputs[WORKSPACE_URL_VARIABLE] = stage.observability.workspace_url
        outputs[RELEASE_PARAMETERS_VARIABLE] = stage.pools.release_parameters_output
        # The stage's workspace, by its tag: every workspace of the account and region, narrowed
        # to the one that carries the stage's name.
        workspaces = scope.format_arn(service="aps", resource="workspace", resource_name="*")
        tagged = {"StringEquals": {f"aws:ResourceTag/{STAGE_TAG}": stage.observability.stage_name}}
        statements.append(iam.PolicyStatement(actions=["aps:QueryMetrics"], resources=[workspaces], conditions=tagged))
        # The release parameter of each pool the stage deploys, by its name, and no other.
        parameters = [
            scope.format_arn(service="ssm", resource="parameter", resource_name=name.lstrip("/"))
            for name in stage.pools.release_parameters.values()
        ]
        if parameters:
            statements.append(iam.PolicyStatement(actions=["ssm:GetParameter"], resources=parameters))
    else:
        # A stage that is not the endpoint's has no endpoint to verify: the step says so, as not run.
        environment[ENDPOINT_VARIABLE] = NO_ENDPOINT
    return pipelines.CodeBuildStep(
        ACTION,
        input=source,
        install_commands=INSTALL_COMMANDS,
        commands=COMMANDS,
        partial_build_spec=codebuild.BuildSpec.from_object(
            {"version": "0.2", "phases": {"post_build": {"commands": POST_BUILD_COMMANDS}}}
        ),
        env=environment,
        env_from_cfn_outputs=outputs,
        role_policy_statements=statements,
    )
