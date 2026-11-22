"""A pool's instance role and its instances' log group (system design, section 4.3).

Instances have no public address and no SSH: debugging is through Session Manager. The role holds
no AWS managed policy. The one AWS publishes for Session Manager would let every instance read
every SSM parameter, the configuration's included (evidence E23). The session's policy is written
here instead, with the five actions Session Manager needs and no other. AWS grants them only on
every resource, so `tests/policy/acknowledged/pools_role.toml` lists each with its reason.

Each grant is made by the issue that creates its resource, once both exist. This module grants
what exists when the role does:

- writing the pool's own rows in the heartbeat table, the rows whose partition key is the pool's
  name;
- remote write to the metrics workspace. The statement is inline rather than the workspace's
  `write_policy`, because a policy of another stack is one the invariants of this stack's
  template cannot read;
- writing logs to the pool's own log group, created here with the fixed name `/rhi/pools/<pool>`,
  so that another stack can name it without a reference to this one;
- completing the lifecycle actions of the pool's own group and recording their heartbeats. Auto
  Scaling puts an identifier it chooses at creation in a group's ARN, so the ARN is named by
  pattern and the group's tag `rhi-pool` narrows it to the pool's group;
- for a GPU pool, reading its weights in the model bucket (section 4.4): the objects under its
  own pool's prefix, and the listing of that prefix, which is how the engine's loader finds the
  files of a revision. The bucket is named by its fixed name (`rhi.pools.model_store`), so the
  statement reads the same to an invariant in any stack. The mock pool has no weights, and its
  role nothing on the bucket. No instance role writes there: the copy job's role alone does.

**The path of the GPU pools' roles.** The promotion job tries a candidate machine image on one
instance, a canary, which it starts from a pool's launch template with the pool's instance role,
and a role that starts an instance must be allowed to pass the instance's role. So that the job's
role may pass the GPU pools' roles and no other, every GPU pool's instance role is under one IAM
path, `GPU_ROLE_PATH`, and the mock pool's role is outside it. A path adds no right to a role: it
is part of the role's ARN, by which another role's `iam:PassRole` statement can name these roles
and no other. The roles keep the names CloudFormation gives them.

The role is tagged with its pool, which `tests/policy/test_instance_role.py` reads: an instance
role may read no parameter and no secret other than its own pool's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from rhi.observability import prometheus
from rhi.pools import model_store
from rhi.pools.group import POOL_TAG

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb

    from rhi.observability.prometheus import PrometheusWorkspace

# What Session Manager needs of an instance, and nothing else (section 4.3).
SESSION_ACTIONS = (
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
)
# An instance writes its pool's heartbeat rows, and reads none.
HEARTBEAT_ACTIONS = ("dynamodb:PutItem", "dynamodb:UpdateItem")
LOG_ACTIONS = ("logs:CreateLogStream", "logs:PutLogEvents")
LIFECYCLE_ACTIONS = ("autoscaling:CompleteLifecycleAction", "autoscaling:RecordLifecycleActionHeartbeat")
# Reading a pool's weights: its objects, and the listing of its prefix.
WEIGHTS_READ_ACTION = "s3:GetObject"
WEIGHTS_LIST_ACTION = "s3:ListBucket"

LOG_RETENTION = logs.RetentionDays.THREE_MONTHS  # Section 6.6 (`rhi.retention`).

# The IAM path of every GPU pool's instance role, and of no other role.
GPU_ROLE_PATH = "/rhi/gpu-pools/"


def log_group_name(pool: str) -> str:
    """The fixed name of a pool's log group."""
    return f"/rhi/pools/{pool}"


class PoolRole(Construct):
    """One pool's instance role, as `role`, its session policy, as `session_policy`, and the log
    group of its instances, as `log_group`. `gpu` says the pool is a GPU pool: its role is under
    `GPU_ROLE_PATH` and reads its pool's prefix of the model bucket."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        pool: str,
        heartbeats: dynamodb.ITable,
        workspace: PrometheusWorkspace,
        gpu: bool = False,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)

        # ty reads the CDK's generated classes as unlike the protocols they implement where a method
        # names a parameter otherwise (`grantee` for `identity`): jsii passes it by position.
        ec2_service = iam.ServicePrincipal("ec2.amazonaws.com")
        self.role = iam.Role(
            self,
            "Role",
            assumed_by=ec2_service,  # ty: ignore[invalid-argument-type]
            description=f"The instances of the pool {pool}.",
            # Without a path a role is under `/`, outside every other path.
            path=GPU_ROLE_PATH if gpu else None,
        )
        cdk.Tags.of(self.role).add(POOL_TAG, pool)

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=log_group_name(pool),
            retention=LOG_RETENTION,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=list(HEARTBEAT_ACTIONS),
                resources=[heartbeats.table_arn],
                # Only the rows whose partition key, the pool, is this pool's.
                conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [pool]}},
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(actions=list(prometheus.WRITE_ACTIONS), resources=[workspace.workspace.attr_arn])
        )
        self.role.add_to_policy(
            iam.PolicyStatement(actions=list(LOG_ACTIONS), resources=[self.log_group.log_group_arn])
        )
        groups = stack.format_arn(
            service="autoscaling",
            resource="autoScalingGroup",
            resource_name="*:autoScalingGroupName/*",
            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=list(LIFECYCLE_ACTIONS),
                resources=[groups],
                conditions={"StringEquals": {f"autoscaling:ResourceTag/{POOL_TAG}": pool}},
            )
        )
        if gpu:
            self.role.add_to_policy(
                iam.PolicyStatement(actions=[WEIGHTS_READ_ACTION], resources=[model_store.objects_arn(stack, pool)])
            )
            self.role.add_to_policy(
                iam.PolicyStatement(
                    actions=[WEIGHTS_LIST_ACTION],
                    resources=[model_store.bucket_arn(stack)],
                    # A listing names the whole bucket: the condition keeps it to the pool's prefix.
                    conditions={"StringLike": {"s3:prefix": f"{model_store.pool_prefix(pool)}*"}},
                )
            )

        self.session_policy = iam.Policy(
            self,
            "SessionPolicy",
            statements=[iam.PolicyStatement(actions=list(SESSION_ACTIONS), resources=["*"])],
            roles=[self.role],  # ty: ignore[invalid-argument-type] - as above
        )
