"""The scaler: one function, one invocation at a time, called every minute (system design, sections 2,
5.1 and 5.4).

The function runs `services/scaler/` (`rhi_scaler.handler`) on arm64, packaged from `uv.lock` by
`rhi.packaging`, with no container. Its reserved concurrency is 1, so two runs never overlap. An
EventBridge rule invokes it every minute; the gateway and the admin may invoke it too, the gateway
through the grant its own construct makes on `function`, the admin with the admin's credentials.
An invocation that waited past a minute is dropped, and a failed one is not retried: the next
minute's run reads everything again.

**Its logs** go to the log group `LOG_GROUP_NAME`, a fixed name, so that Grafana's role can name it
without a reference to this stack.

**Its settings** are one JSON object in `RHI_SCALER_SETTINGS`, in the shape `rhi_scaler.settings`
reads: every pool's entry as the stage loaded it, the name of the pool's group and the ARN of its
target group, the names of the heartbeat and pool-state tables, and the workspace's remote-write
address.

**Its role** is the only one of the stage that may set a group's capacity
(`tests/policy/test_single_writer.py`). It has no AWS managed policy. What it may do, each with its
reason:

- `autoscaling:SetDesiredCapacity`, `SetInstanceProtection`, `SuspendProcesses` and
  `ResumeProcesses`, on this stack's pool groups only: Auto Scaling puts an identifier it chooses at
  creation before the name in a group's ARN, so the statement names groups by pattern, and its
  conditions narrow it to a group that CloudFormation created in this stack (the tag
  `aws:cloudformation:stack-id`, which only CloudFormation sets) and that carries a pool's tag
  `rhi-pool`. These are the calls `rhi_scaler.aws_io.apply` makes.
- `autoscaling:DescribeAutoScalingGroups`, `DescribeWarmPool`, `DescribeScalingActivities` and
  `elasticloadbalancing:DescribeTargetHealth`, on every resource, the only way AWS grants them:
  what `rhi_scaler.aws_io.read` reads of each group and its target group.
- reading and writing the items of the heartbeat and pool-state tables, by their ARNs.
- writing its own logs, to its own log group.
- `aps:RemoteWrite` on the metrics workspace alone, the action of the workspace's `write_policy`,
  written in the role's own policy: a run pushes the fleet's metrics.
- `cloudwatch:PutMetricData` on every resource, the only way AWS grants it, under the condition
  that the namespace is `rhi_scaler.cloudwatch.NAMESPACE` and no other: a run publishes what the
  alarms read (section 7.3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from rhi import packaging
from rhi.access.issuer import READ_ACTIONS, WRITE_ACTIONS
from rhi.config import pool_entry
from rhi.observability import prometheus
from rhi.pools.group import POOL_TAG
from rhi_scaler.cloudwatch import NAMESPACE as METRICS_NAMESPACE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    from rhi.observability.prometheus import PrometheusWorkspace
    from rhi.pools.group import PoolGroup
    from rhi_common.pool_config import Pool

HANDLER = "rhi_scaler.handler.handler"
ARCHITECTURE = lambda_.Architecture.ARM_64
# A run must end before the next one starts.
TIMEOUT = cdk.Duration.seconds(50)
MEMORY_MB = 256
RESERVED_CONCURRENCY = 1
SCHEDULE = events.Schedule.rate(cdk.Duration.minutes(1))
# An invocation older than a minute is one the next run replaces.
MAX_EVENT_AGE = cdk.Duration.minutes(1)

LOG_GROUP_NAME = "/rhi/scaler"
LOG_RETENTION = logs.RetentionDays.THREE_MONTHS  # Section 6.6 (`rhi.retention`).

SETTINGS_VARIABLE = "RHI_SCALER_SETTINGS"
# The workspace's endpoint ends with a slash.
REMOTE_WRITE_PATH = "api/v1/remote_write"

# The calls that change a group, and those that only read, which AWS grants on every resource only.
WRITE_GROUP_ACTIONS = [
    "autoscaling:ResumeProcesses",
    "autoscaling:SetDesiredCapacity",
    "autoscaling:SetInstanceProtection",
    "autoscaling:SuspendProcesses",
]
DESCRIBE_ACTIONS = [
    "autoscaling:DescribeAutoScalingGroups",
    "autoscaling:DescribeScalingActivities",
    "autoscaling:DescribeWarmPool",
    "elasticloadbalancing:DescribeTargetHealth",
]
# Granted on every resource only, and narrowed by its condition to the scaler's namespace.
PUBLISH_ACTIONS = ["cloudwatch:PutMetricData"]
# The tag CloudFormation puts on every group it creates, with the ID of the group's stack.
STACK_ID_TAG = "aws:cloudformation:stack-id"


def log_group(scope: Construct) -> logs.LogGroup:
    """The scaler's log group, of a fixed name, as the child `Logs` of `scope`: the construct's own, or,
    for a parked stage, a construct of the same ID that holds the log group alone, so that it keeps
    its logical ID while what writes to it is gone (system design, section 5.6)."""
    return logs.LogGroup(
        scope,
        "Logs",
        log_group_name=LOG_GROUP_NAME,
        retention=LOG_RETENTION,
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )


class Scaler(Construct):
    """The scaler's function, as `function`, its role, as `role`, its log group, as `log_group`, and
    the rule that invokes it every minute, as `schedule`. Another construct allows its own role to
    invoke `function`; this one grants that to no role."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        pools: tuple[Pool, ...],
        groups: Mapping[str, PoolGroup],
        target_groups: Mapping[str, elbv2.IApplicationTargetGroup],
        heartbeats: dynamodb.ITable,
        pool_state: dynamodb.ITable,
        workspace: PrometheusWorkspace,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)

        # ty reads the CDK's generated classes as unlike the protocols they implement where a method
        # names a parameter otherwise (`grantee` for `identity`): jsii passes it by position.
        lambda_service = iam.ServicePrincipal("lambda.amazonaws.com")
        # Its own role, with no AWS managed policy: what the function may do is listed here, whole.
        self.role = iam.Role(self, "Role", assumed_by=lambda_service)  # ty: ignore[invalid-argument-type]
        self.log_group = log_group(self)
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[self.log_group.log_group_arn]
            )
        )
        if pools:
            groups_arn = stack.format_arn(
                service="autoscaling",
                resource="autoScalingGroup",
                resource_name="*:autoScalingGroupName/*",
                arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
            )
            self.role.add_to_policy(
                iam.PolicyStatement(
                    actions=WRITE_GROUP_ACTIONS,
                    resources=[groups_arn],
                    conditions={
                        "StringEquals": {
                            f"autoscaling:ResourceTag/{STACK_ID_TAG}": stack.stack_id,
                            f"autoscaling:ResourceTag/{POOL_TAG}": [pool.name for pool in pools],
                        }
                    },
                )
            )
        self.role.add_to_policy(iam.PolicyStatement(actions=DESCRIBE_ACTIONS, resources=["*"]))
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=READ_ACTIONS + WRITE_ACTIONS, resources=[heartbeats.table_arn, pool_state.table_arn]
            )
        )
        # Inline rather than the workspace's `write_policy`: a policy of another stack is one the
        # invariants of this stack's template cannot read.
        self.role.add_to_policy(
            iam.PolicyStatement(actions=list(prometheus.WRITE_ACTIONS), resources=[workspace.workspace.attr_arn])
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=PUBLISH_ACTIONS,
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": METRICS_NAMESPACE}},
            )
        )

        settings = {
            "pools": {
                pool.name: {
                    "entry": pool_entry(pool),
                    "group": groups[pool.name].group.ref,
                    "target_group": target_groups[pool.name].target_group_arn,
                }
                for pool in pools
            },
            "heartbeat_table": heartbeats.table_name,
            "pool_state_table": pool_state.table_name,
            "workspace_url": cdk.Fn.join("", [workspace.workspace.attr_prometheus_endpoint, REMOTE_WRITE_PATH]),
        }

        self.function = lambda_.Function(
            self,
            "Function",
            runtime=packaging.RUNTIME,
            architecture=ARCHITECTURE,
            handler=HANDLER,
            code=packaging.function_code("scaler", ARCHITECTURE),
            role=self.role,  # ty: ignore[invalid-argument-type] - as above
            log_group=self.log_group,
            memory_size=MEMORY_MB,
            timeout=TIMEOUT,
            reserved_concurrent_executions=RESERVED_CONCURRENCY,
            max_event_age=MAX_EVENT_AGE,
            retry_attempts=0,
            environment={SETTINGS_VARIABLE: stack.to_json_string(settings)},
        )
        # The function names its role, and the CDK makes it depend on the role as well, which cfn-lint
        # warns about (W3005): it keeps its dependency on the role's policy alone.
        policy = self.role.node.find_child("DefaultPolicy").node.default_child
        function = self.function.node.default_child
        assert isinstance(policy, cdk.CfnResource) and isinstance(function, cdk.CfnResource)
        function.add_override("DependsOn", [stack.get_logical_id(policy)])

        self.schedule = events.Rule(
            self,
            "Schedule",
            schedule=SCHEDULE,
            targets=[targets.LambdaFunction(self.function, retry_attempts=0)],  # ty: ignore[invalid-argument-type]
        )
