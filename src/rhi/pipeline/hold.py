"""The pipeline's step that holds the stage's deployment while a session is on (system design,
sections 5.6, 8.2 and 8.4).

There is one environment: a deployment that touches a pool refreshes its instances, which in a
class would cost the students their warm instances and cached transcripts, and a gateway
deployment drains for up to 30 minutes (sections 4.3 and 8.5). So among the stage's steps before
its deployment, the step runs `scripts/pipeline/deploy_hold.py`, on the source, in a locked
environment: it reads the session switch and passes once it reads off, at once when it already
does, when its item is missing, or when the pools stack or its table does not exist yet. While it
reads on, the job reads it again every 60 seconds. The pipeline has no manual approval: this step
is what keeps a deployment from reaching a class.

**The action is named `ACTION`.** `tests/policy/test_pipeline.py` lists it, after the copy of the
weights and before the stage's deployment actions, and holds its role to what is written below.

**How long it waits.** A session is a few hours (assumption A2). The step's timeout is CodeBuild's
limit for one build, `TIMEOUT_HOURS`, so that only a session left on for a day and a half fails the
execution (section 17).

**What the step is told.** `RHI_HOLD_POOLS_STACK` is the pools stack's name and
`RHI_HOLD_TABLE_LOGICAL_ID` the pool-state table's logical ID in it, both read from the stage as
text: the pipeline stack, another assembly, holds no reference to a resource of the stage
(`rhi.pipeline.spikes.pool_state_table_arn` reads the table the same way). The job finds the
table's name by that logical ID, as the admin CLI does. `PYTHONPATH` holds the folders of pytest's
import path, as in the synth step: the job imports `rhi_common`.

**The role** may read the one stack resource that names the table,
`cloudformation:DescribeStackResource` on the pools stack, and read the table's items,
`dynamodb:GetItem` on the pool-state table, named by the pools stack's name, its logical ID and
the suffix CloudFormation generates. Beside that it holds what CDK Pipelines gives every project of
the pipeline: its own logs, its own test reports and a read of its input in the artifact bucket.
Nothing else: `tests/synth/test_pipeline_hold.py` and `tests/policy/test_pipeline.py` hold it to that.

The step is a step, not a construct: CDK Pipelines creates its project and role inside the
pipeline construct of `rhi.pipeline.stack`, whose acknowledgement file covers them. This module
creates no resource.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import pipelines

from rhi.pipeline.spikes import MAX_TIMEOUT_HOURS, pool_state_table_arn
from rhi.pipeline.synth import UV_VERSION, import_path
from rhi.stage import EndpointStage

# The name of the step's action.
ACTION = "Hold"
SCRIPT = "scripts/pipeline/deploy_hold.py"
# The variables that name the pools stack and the pool-state table's logical ID to the job.
STACK_VARIABLE = "RHI_HOLD_POOLS_STACK"
LOGICAL_ID_VARIABLE = "RHI_HOLD_TABLE_LOGICAL_ID"
# CodeBuild's limit for one build.
TIMEOUT_HOURS = MAX_TIMEOUT_HOURS

INSTALL_COMMANDS = [f"python3 -m pip install uv=={UV_VERSION}"]
COMMANDS = ["uv sync --locked", f"uv run --locked python {SCRIPT}"]


def pool_state_logical_id(stage: EndpointStage) -> str:
    """The logical ID of the pool-state table in the pools stack of `stage`, as text."""
    pools = stage.pools
    table = pools.tables.pool_state.node.default_child
    assert isinstance(table, cdk.CfnElement)
    logical_id = pools.resolve(table.logical_id)
    assert isinstance(logical_id, str)
    return logical_id


def hold_step(scope: cdk.Stack, source: pipelines.IFileSetProducer, stage: EndpointStage) -> pipelines.CodeBuildStep:
    """The step of the pipeline of `scope` that holds the deployment of `stage` while a session is on, on `source`."""
    pools_stack = scope.format_arn(
        service="cloudformation", resource="stack", resource_name=f"{stage.pools.stack_name}/*"
    )
    return pipelines.CodeBuildStep(
        ACTION,
        input=source,
        install_commands=INSTALL_COMMANDS,
        commands=COMMANDS,
        env={
            STACK_VARIABLE: stage.pools.stack_name,
            LOGICAL_ID_VARIABLE: pool_state_logical_id(stage),
            "PYTHONPATH": ":".join(import_path()),
        },
        role_policy_statements=[
            # The table's name, by its logical ID in the pools stack: that one stack's resources.
            iam.PolicyStatement(actions=["cloudformation:DescribeStackResource"], resources=[pools_stack]),
            # The session switch, one item of the pool-state table.
            iam.PolicyStatement(actions=["dynamodb:GetItem"], resources=[pool_state_table_arn(scope, stage)]),
        ],
        timeout=cdk.Duration.hours(TIMEOUT_HOURS),
    )
