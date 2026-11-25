"""The pipeline's verification step (system design, section 8.2, step 6).

After the stage is deployed, the step runs the smoke tests against the deployed endpoint with the
canary's key: `scripts/pipeline/verify.py`, on the source, in a locked environment. It builds no
image and runs outside the private network. A failed test fails the step, and with it the
execution (section 8.5).

**The coursework pool.** Before the smoke tests, the step runs the decision of
`scripts/pipeline/gpu_verification.py`: whether this execution deployed a GPU pool's release,
which an instance refresh of a GPU pool's group that began after the execution started says
(sections 4.3 and 8.2, step 6). The decision waits for such a refresh to end, and fails the step
when it did not end well; its answer is a file, `RHI_VERIFY_GPU_POOLS_FILE`, that names
`coursework` or nothing. Its command ends before the verification's begins, so the smoke tests
then run on the mock pool and, when the file names it, on the coursework pool, after the refresh.

**The session on the mock pool.** The script books the mock pool before its run of the tests on it,
one instance under the name `pipeline/verify` for twice the configuration's `check_wait_minutes`,
waits until the gateway's status reads the pool ready or `check_wait_minutes` have passed, and
stops the session it booked after the run, whatever its result (`scripts/pipeline/booking.py`).
It books only a pool that has no session booked or active, and stops only the session it booked.
So the deployed verification does not depend on a wake. The session is an item of the stage's
pool-state table, which the script finds as the deployment hold does (`rhi.pipeline.hold`), by
the table's logical ID in the pools stack.

**How long the step runs.** Its timeout is `timeout`: `TIMEOUT`, which covers the set-up, a refresh
and two runs of the tests, each with a cold wake, and `check_wait_minutes` more, the longest the
script waits for the session's instance. So the build is not cut before the wait is over, the tests
have run and the session is stopped. A wait that would take the timeout beyond CodeBuild's limit for
one build is refused at synthesis.

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
- `RHI_VERIFY_HEARTBEATS_TABLE`: the name of the stage's heartbeats table, the pools stack's too.

of the stage itself, read at synthesis as text, as the hold's step reads them:

- `RHI_VERIFY_POOLS_STACK`: the pools stack's name;
- `RHI_VERIFY_POOL_STATE_LOGICAL_ID`: the pool-state table's logical ID in that stack;
- `RHI_VERIFY_CHECK_WAIT_MINUTES`: the configuration's `check_wait_minutes`;
- `RHI_VERIFY_SESSION_MAX_HOURS`: the configuration's `session_max_hours`, the bound a session is
  written and read under (`rhi_common.sessions`);
- `PYTHONPATH`: the folders of pytest's import path, as in the hold's step: the booking imports
  `rhi_common`.

and of the execution:

- `RHI_VERIFY_EXECUTION`: the execution's ID, from the pipeline's variable for it;
- `RHI_VERIFY_BUCKET`: the pipeline's artifact bucket;
- `RHI_VERIFY_REASON_FILE`: where the script leaves its reason when it runs no test;
- `RHI_VERIFY_FAILED_TESTS_FILE`: where the script leaves the IDs of the tests that fail;
- `RHI_VERIFY_LAST_STAGES_FILE`: where the script leaves the last stage each instance reached;
- `RHI_VERIFY_GPU_POOLS_FILE`: where the decision leaves the pools the smoke tests also run on.

The decision learns the pipeline's name from CodeBuild's own `CODEBUILD_INITIATOR`: the pipeline
refers to the step's project, so the project's environment may not refer to the pipeline.

**When no test runs.** While the endpoint has no listener, or the canary's secret is missing or
holds no key, the script runs no test, writes its reason to that file, `listener-off` or
`canary-missing`, and exits 0. The step's post-build phase copies the file, when there is one, to
the object `verification/<execution ID>/not-run` of the artifact bucket, where the reporter reads
that the verification was not run. A copy that fails fails the step: a verification that was not
run is never taken for one that passed.

**While the system is parked** (the configuration's `parked`, sections 8.5 and 11.2), the stage
has no endpoint, and the step is built without the smoke tests: the pipeline is synthesized from
the stage's configuration, so the step learns the flag from its definition, never from a read of
SSM when it runs. It is still the action `ACTION`, after every deployment action of the stage, on
the source. Its build phase runs neither the decision nor the script, nor installs anything; its
post-build phase writes the one line `parked`, `PARKED`, to the reason file and copies it, as above,
to `verification/<execution ID>/not-run`, so the reporter posts the description of a verification
that passed because the system is parked, never that of one whose tests ran. Nothing goes to the
step's output artifact, and a copy that fails fails the step. Its role may then only write under
`verification/` of the artifact bucket, beside what CDK Pipelines gives every project.

**When a test fails.** A CodeBuild action that fails may leave no output artifact, so the step
does not rely on one. The script leaves the ID of each smoke test that fails, one per line and
nothing else, in the file of the second variable, and the post-build phase, which CodeBuild runs
whether the build phase passed or failed, copies it, when there is one, to the object
`verification/<execution ID>/failed-smoke-tests`, beside `not-run`. The reporter names those
tests in its report of the failure (`rhi_reporter.handler`).

**Where a start stopped.** A failed run of the tests also has the script read the heartbeat rows
of the instances of the pools it tested, and leave the last lifecycle stage each one reached, one
stage per line and nothing else of a row, in the file of the last variable. The post-build phase
copies it, when there is one, to the object `verification/<execution ID>/last-stages`. The
reporter names those stages in its report, the ones its own list holds (section 8.4): it is what
tells the factory, which holds no AWS credentials, where a start that never served stopped.

**How the step addresses the heartbeats table.** The table is the pools stack's, in the stage's
assembly, and the pipeline stack can hold no reference to it: CDK refuses a reference between two
assemblies. An export read with an import would not do either: the pipeline updates itself before
the stage deploys, and its first update would wait for an export that does not exist yet. So the
table's name reaches the step when it runs, as an output of the pools stack, and the role's grant
names the table by an ARN built here, at synthesis: the pipeline stack's own partition, region and
account, which are the stage's, since the stage names no environment, and the name CloudFormation
gives a table that has none of its own: the stack's name, a hyphen, the table's logical ID, a
hyphen and a suffix it generates. The stack's name and the logical ID are read from the stage as
text. A logical ID holds no hyphen, so the pattern matches that one table
(`tests/synth/test_pipeline_verify.py`).

**The role** may read the canary's secret, `rhi/canary-key`; query the workspace of the stage it
verifies, which it names by the workspace's tag, since the workspace has no ID when the role is
written (`rhi.observability.stack`); read the release parameter of each pool the stage deploys, by
its name; read the items of the stage's heartbeats table by pool, `dynamodb:Query` and no other
action on it; read the one stack resource that names the stage's pool-state table,
`cloudformation:DescribeStackResource` on the pools stack; read and write, `dynamodb:GetItem` and
`dynamodb:PutItem`, the items of that table whose partition key is the mock pool's name, and no
other pool's: the condition is on `dynamodb:LeadingKeys`, in the form the canary's role has for
the heartbeats table (`rhi.pipeline.canary`). That covers every record of the mock pool's
partition, its session among them, and neither the coursework pool's records nor the session
switch; and write under `verification/` of the artifact bucket. Beside that it holds what CDK
Pipelines gives every project of the pipeline: its own logs, its own test reports and a read of
its input in the artifact bucket. For the decision, it may also describe Auto Scaling groups and
their instance refreshes, which AWS grants on every resource only, and list the executions of this
pipeline, by its ARN, and of no other. Nothing else: `tests/policy/test_pipeline.py` holds it to that.

The step is a step, not a construct: CDK Pipelines creates its project and role inside the
pipeline construct of `rhi.pipeline.stack`, whose acknowledgement file covers them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_iam as iam
from aws_cdk import pipelines

from rhi.gateway.stack import NO_ENDPOINT
from rhi.observability.stack import STAGE_TAG
from rhi.pipeline.hold import pool_state_logical_id
from rhi.pipeline.spikes import MAX_TIMEOUT_HOURS, pool_state_table_arn
from rhi.pipeline.synth import UV_VERSION, import_path
from rhi.stage import EndpointStage

if TYPE_CHECKING:
    from aws_cdk import aws_s3 as s3

# The name of the step's action, which the reporter reads.
ACTION = "Verify"
SCRIPT = "scripts/pipeline/verify.py"
# The decision whether the execution verifies on the coursework pool, which runs before `SCRIPT`.
DECISION = "scripts/pipeline/gpu_verification.py"
# The secret the admin CLI's canary command writes the canary's key into (`rhi_admin.canary`).
CANARY_SECRET = "rhi/canary-key"
# Where, in the artifact bucket, the reporter reads what a verification left: under this prefix,
# in the folder of the execution's ID.
PREFIX = "verification"
NOT_RUN = "not-run"
# The reason the step itself leaves, while the system is parked, in place of the script's.
PARKED = "parked"
FAILED_TESTS = "failed-smoke-tests"
LAST_STAGES = "last-stages"
# The files the script leaves its reason, the IDs of the failed tests and the instances' last
# stages in, outside the checkout.
REASON_FILE = "/tmp/rhi-verification-not-run"
FAILED_TESTS_FILE = "/tmp/rhi-verification-failed-smoke-tests"
LAST_STAGES_FILE = "/tmp/rhi-verification-last-stages"
GPU_POOLS_FILE = "/tmp/rhi-verification-gpu-pools"

ENDPOINT_VARIABLE = "RHI_SMOKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SMOKE_WORKSPACE_URL"
RELEASE_PARAMETERS_VARIABLE = "RHI_VERIFY_RELEASE_PARAMETERS"
HEARTBEATS_TABLE_VARIABLE = "RHI_VERIFY_HEARTBEATS_TABLE"
EXECUTION_VARIABLE = "RHI_VERIFY_EXECUTION"
BUCKET_VARIABLE = "RHI_VERIFY_BUCKET"
REASON_FILE_VARIABLE = "RHI_VERIFY_REASON_FILE"
FAILED_TESTS_FILE_VARIABLE = "RHI_VERIFY_FAILED_TESTS_FILE"
LAST_STAGES_FILE_VARIABLE = "RHI_VERIFY_LAST_STAGES_FILE"
GPU_POOLS_FILE_VARIABLE = "RHI_VERIFY_GPU_POOLS_FILE"
# What the script books the mock pool with: the pools stack's name and the pool-state table's
# logical ID in it, and the configuration's `check_wait_minutes` and `session_max_hours`.
POOLS_STACK_VARIABLE = "RHI_VERIFY_POOLS_STACK"
POOL_STATE_LOGICAL_ID_VARIABLE = "RHI_VERIFY_POOL_STATE_LOGICAL_ID"
CHECK_WAIT_MINUTES_VARIABLE = "RHI_VERIFY_CHECK_WAIT_MINUTES"
SESSION_MAX_HOURS_VARIABLE = "RHI_VERIFY_SESSION_MAX_HOURS"
# The pool the script books, by its name: the partition key of the only items of the pool-state
# table the role may read and write, with these two actions.
MOCK_POOL = "mock"
POOL_STATE_ITEMS = ["dynamodb:GetItem", "dynamodb:PutItem"]
# CodePipeline's variable for the ID of the execution an action runs in.
EXECUTION_ID = "#{codepipeline.PipelineExecutionId}"

INSTALL_COMMANDS = [f"python3 -m pip install uv=={UV_VERSION}"]
COMMANDS = ["uv sync --locked", f"uv run --locked python {DECISION}", f"uv run --locked python {SCRIPT}"]
# What the decision reads of Auto Scaling, which grants it on every resource only.
DESCRIBES_REFRESHES = ["autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeInstanceRefreshes"]
# The longest the step runs beside its wait for the instance of the session it booked: the set-up,
# the decision's wait for a refresh, an hour at most, and the smoke tests on the mock pool and on
# the coursework pool, each with a cold wake and the workspace's lag.
TIMEOUT = cdk.Duration.hours(3)


def timeout(check_wait_minutes: int) -> cdk.Duration:
    """The step's timeout: `TIMEOUT` and the wait for the session's instance, `check_wait_minutes`,
    so that the build outlasts the wait, the tests and the stop of the session. A `ValueError` when
    that is longer than CodeBuild lets one build run."""
    minutes = TIMEOUT.to_minutes() + check_wait_minutes
    if minutes > MAX_TIMEOUT_HOURS * 60:
        raise ValueError(
            f"check_wait_minutes is {check_wait_minutes}: with the {TIMEOUT.to_minutes()} minutes of the "
            f"verification's other work, the step would run longer than CodeBuild's {MAX_TIMEOUT_HOURS} hours"
        )
    return cdk.Duration.minutes(minutes)


def _copy(file_variable: str, key: str) -> str:
    """The command that copies the file a variable names, when there is one, to the object `key`
    of the execution's folder, where the reporter reads it."""
    to = f"s3://${BUCKET_VARIABLE}/{PREFIX}/${EXECUTION_VARIABLE}/{key}"
    return f'if [ -f "${file_variable}" ]; then aws s3 cp "${file_variable}" "{to}"; fi'


# What the script left, as the objects the reporter reads: its reason, the tests that failed, and
# the last stage each instance reached.
POST_BUILD_COMMANDS = [
    _copy(REASON_FILE_VARIABLE, NOT_RUN),
    _copy(FAILED_TESTS_FILE_VARIABLE, FAILED_TESTS),
    _copy(LAST_STAGES_FILE_VARIABLE, LAST_STAGES),
]


# While parked: no test, and the reason, written then copied where the reporter reads it.
PARKED_COMMANDS = ["echo 'The system is parked: it has no endpoint, so no test runs.'"]
PARKED_POST_BUILD_COMMANDS = [f'echo {PARKED} > "${REASON_FILE_VARIABLE}"', _copy(REASON_FILE_VARIABLE, NOT_RUN)]


def heartbeats_table_arn(scope: cdk.Stack, stage: EndpointStage) -> str:
    """The ARN pattern of the heartbeats table of `stage`, in the partition, region and account of
    `scope`, the pipeline's stack: the table by the name CloudFormation gives it, the pools stack's
    name, a hyphen, the table's logical ID, a hyphen and the suffix CloudFormation generates."""
    pools = stage.pools
    table = pools.tables.heartbeats.node.default_child
    assert isinstance(table, cdk.CfnElement)
    # The logical ID as text: a reference to it would be one between two assemblies.
    logical_id = pools.resolve(table.logical_id)
    assert isinstance(logical_id, str)
    return scope.format_arn(service="dynamodb", resource="table", resource_name=f"{pools.stack_name}-{logical_id}-*")


def verify_step(
    scope: cdk.Stack,
    source: pipelines.IFileSetProducer,
    *,
    stage: cdk.Stage,
    artifacts: s3.IBucket,
    pipeline: codepipeline.Pipeline,
    parked: bool = False,
) -> pipelines.CodeBuildStep:
    """The verification step of `pipeline`, in `scope`, on `source`, for `stage`, the stage it
    runs after, in a pipeline whose artifact bucket is `artifacts`; while `parked`, the step that
    runs no test and says so."""
    if parked:
        return _parked_step(source, artifacts)
    environment = {
        EXECUTION_VARIABLE: EXECUTION_ID,
        BUCKET_VARIABLE: artifacts.bucket_name,
        REASON_FILE_VARIABLE: REASON_FILE,
        FAILED_TESTS_FILE_VARIABLE: FAILED_TESTS_FILE,
        LAST_STAGES_FILE_VARIABLE: LAST_STAGES_FILE,
        GPU_POOLS_FILE_VARIABLE: GPU_POOLS_FILE,
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
        # The decision: the groups and their refreshes, and when this pipeline's execution started.
        iam.PolicyStatement(actions=DESCRIBES_REFRESHES, resources=["*"]),
        iam.PolicyStatement(actions=["codepipeline:ListPipelineExecutions"], resources=[pipeline.pipeline_arn]),
    ]
    if isinstance(stage, EndpointStage):
        outputs[ENDPOINT_VARIABLE] = stage.gateway.endpoint
        outputs[WORKSPACE_URL_VARIABLE] = stage.observability.workspace_url
        outputs[RELEASE_PARAMETERS_VARIABLE] = stage.pools.release_parameters_output
        outputs[HEARTBEATS_TABLE_VARIABLE] = stage.pools.heartbeats_table_output
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
        # The stage's heartbeat rows, by pool: a query of that one table, and nothing else of it.
        statements.append(
            iam.PolicyStatement(actions=["dynamodb:Query"], resources=[heartbeats_table_arn(scope, stage)])
        )
        # The session on the mock pool: where the pool-state table is, as text, and the two
        # numbers of the configuration the booking is made with.
        environment |= {
            POOLS_STACK_VARIABLE: stage.pools.stack_name,
            POOL_STATE_LOGICAL_ID_VARIABLE: pool_state_logical_id(stage),
            CHECK_WAIT_MINUTES_VARIABLE: str(stage.config.check_wait_minutes),
            SESSION_MAX_HOURS_VARIABLE: str(stage.config.session_max_hours),
            "PYTHONPATH": ":".join(import_path()),
        }
        wait_minutes = stage.config.check_wait_minutes
        # The table's name, by its logical ID in the pools stack: that one stack's resources.
        pools_stack = scope.format_arn(
            service="cloudformation", resource="stack", resource_name=f"{stage.pools.stack_name}/*"
        )
        statements.append(
            iam.PolicyStatement(actions=["cloudformation:DescribeStackResource"], resources=[pools_stack])
        )
        # The mock pool's items of the pool-state table, its session among them, read and written:
        # the partition key may only be that pool's name.
        statements.append(
            iam.PolicyStatement(
                actions=POOL_STATE_ITEMS,
                resources=[pool_state_table_arn(scope, stage)],
                conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [MOCK_POOL]}},
            )
        )
    else:
        # A stage that is not the endpoint's has no endpoint to verify: the step says so, as not run.
        environment[ENDPOINT_VARIABLE] = NO_ENDPOINT
        wait_minutes = 0
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
        timeout=timeout(wait_minutes),
    )


def _parked_step(source: pipelines.IFileSetProducer, artifacts: s3.IBucket) -> pipelines.CodeBuildStep:
    """The verification step while the system is parked: no test, and the reason `PARKED` written
    to the execution's not-run mark in the post-build phase."""
    return pipelines.CodeBuildStep(
        ACTION,
        input=source,
        commands=PARKED_COMMANDS,
        partial_build_spec=codebuild.BuildSpec.from_object(
            {"version": "0.2", "phases": {"post_build": {"commands": PARKED_POST_BUILD_COMMANDS}}}
        ),
        env={
            EXECUTION_VARIABLE: EXECUTION_ID,
            BUCKET_VARIABLE: artifacts.bucket_name,
            REASON_FILE_VARIABLE: REASON_FILE,
        },
        role_policy_statements=[
            iam.PolicyStatement(actions=["s3:PutObject"], resources=[artifacts.arn_for_objects(f"{PREFIX}/*")])
        ],
    )
