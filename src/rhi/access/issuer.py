"""The key issuer: a function behind the public load balancer, throttled, with privileges apart from
the data path (system design, sections 2 and 6.4).

The function runs `services/issuer/` (`rhi_issuer.handler`) on arm64, packaged from `uv.lock` by
`rhi.packaging`. Its throttles here are its reserved concurrency, the configuration's
`issuer_reserved_concurrency`, and the daily limit of issuances per student it is told; it refuses
malformed input itself, and the WAF rate rule on its path is the edge's.

**How it is reached.** One rule of the public load balancer's HTTPS listener, at priority
`PRIORITY`, forwards the paths under `/auth/` to the function's target group. The gateway's rule
is at 10 and Grafana's at 30. The listener exists only while the configuration's `https_listener`
is true (`rhi.network.edge`): without it the function has no target group, no rule and no
permission for the load balancer to invoke it, and nothing reaches it from the internet.

**Its logs** go to the log group `LOG_GROUP_NAME`, a fixed name, so that Grafana's role can name it
without a reference to this stack.

**Its role** may read the roster table, read and write the keys table and query the keys table's
index of a student's keys, by its name, and write its own logs. Nothing else: not the roster's
index, not the usage table, no secret, not the configuration. The statements name each resource
whole, so no wildcard needs acknowledging.

What the function is told, as environment variables (`rhi_issuer.handler`):

- `RHI_CLIENT_ID`: the login app's client ID, from the configuration;
- `RHI_COURSE_END`: the course end date, YYYY-MM-DD, from the configuration;
- `RHI_ISSUANCES_PER_DAY`: the configuration's `key_issuances_per_day`;
- `RHI_ROSTER_TABLE` and `RHI_KEYS_TABLE`: the names the deployment gives the two tables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_targets as elb_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from rhi import packaging
from rhi_common.tables import BY_STUDENT, KEYS

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb

    from rhi.config import Config

# What reading and writing a table's items take: a transaction is authorized by the actions of
# its items. Reading a table's description is not needed, nor are its streams.
READ_ACTIONS = [
    "dynamodb:BatchGetItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
]
WRITE_ACTIONS = ["dynamodb:BatchWriteItem", "dynamodb:DeleteItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]

HANDLER = "rhi_issuer.handler.handler"
ARCHITECTURE = lambda_.Architecture.ARM_64
# A login waits on GitHub, at most 10 seconds a call (`rhi_issuer.redeem`), then on the tables.
TIMEOUT = cdk.Duration.seconds(30)
MEMORY_MB = 256

LOG_GROUP_NAME = "/rhi/issuer"
LOG_RETENTION = logs.RetentionDays.ONE_MONTH

# The listener's rules need distinct priorities; the design gives none (gateway 10, Grafana 30).
PRIORITY = 20
PATH_PATTERN = "/auth/*"

CLIENT_ID_VARIABLE = "RHI_CLIENT_ID"
COURSE_END_VARIABLE = "RHI_COURSE_END"
ISSUANCES_PER_DAY_VARIABLE = "RHI_ISSUANCES_PER_DAY"
ROSTER_TABLE_VARIABLE = "RHI_ROSTER_TABLE"
KEYS_TABLE_VARIABLE = "RHI_KEYS_TABLE"


class Issuer(Construct):
    """The issuer's function, as `function`, and, when `listener` is given, its target group and
    its rule on that listener, as `target_group` and `rule`; both are None without one."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        roster: dynamodb.ITable,
        keys: dynamodb.ITable,
        listener: elbv2.IApplicationListener | None,
    ) -> None:
        super().__init__(scope, construct_id)

        # ty reads the CDK's generated classes as unlike the protocols they implement where a method
        # names a parameter otherwise (`grantee` for `identity`): jsii passes it by position.
        lambda_service = iam.ServicePrincipal("lambda.amazonaws.com")
        # Its own role, with no managed policy: what the function may do is listed here, whole.
        role = iam.Role(self, "Role", assumed_by=lambda_service)  # ty: ignore[invalid-argument-type]
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=LOG_GROUP_NAME,
            retention=LOG_RETENTION,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[log_group.log_group_arn]
            )
        )
        role.add_to_policy(iam.PolicyStatement(actions=READ_ACTIONS, resources=[roster.table_arn]))
        role.add_to_policy(
            iam.PolicyStatement(actions=READ_ACTIONS + WRITE_ACTIONS, resources=[keys.table_arn])
        )
        # The one index the issuer queries, a student's keys (`rhi_issuer.rules`), by its name.
        by_student = KEYS.index(BY_STUDENT).name
        role.add_to_policy(
            iam.PolicyStatement(actions=["dynamodb:Query"], resources=[f"{keys.table_arn}/index/{by_student}"])
        )

        self.function = lambda_.Function(
            self,
            "Function",
            runtime=packaging.RUNTIME,
            architecture=ARCHITECTURE,
            handler=HANDLER,
            code=packaging.function_code("issuer", ARCHITECTURE),
            role=role,  # ty: ignore[invalid-argument-type] - as above
            log_group=log_group,
            memory_size=MEMORY_MB,
            timeout=TIMEOUT,
            reserved_concurrent_executions=config.issuer_reserved_concurrency,
            environment={
                CLIENT_ID_VARIABLE: config.login_client_id,
                COURSE_END_VARIABLE: config.course_end.isoformat(),
                ISSUANCES_PER_DAY_VARIABLE: str(config.key_issuances_per_day),
                ROSTER_TABLE_VARIABLE: roster.table_name,
                KEYS_TABLE_VARIABLE: keys.table_name,
            },
        )
        # The function names its role, and the CDK makes it depend on the role as well, which cfn-lint
        # warns about (W3005): it keeps its dependency on the role's policy alone.
        policy = role.node.find_child("DefaultPolicy").node.default_child
        function = self.function.node.default_child
        assert isinstance(policy, cdk.CfnResource) and isinstance(function, cdk.CfnResource)
        function.add_override("DependsOn", [cdk.Stack.of(self).get_logical_id(policy)])

        self.target_group: elbv2.ApplicationTargetGroup | None = None
        self.rule: elbv2.ApplicationListenerRule | None = None
        if listener is None:
            return
        # The target adds the permission for Elastic Load Balancing to invoke the function, which the
        # target group needs before it registers the function. Multi-value headers stay off, as the
        # handler expects.
        target = elb_targets.LambdaTarget(self.function)  # ty: ignore[invalid-argument-type] - as above
        self.target_group = elbv2.ApplicationTargetGroup(self, "Target", targets=[target])
        self.rule = elbv2.ApplicationListenerRule(
            self,
            "Rule",
            listener=listener,
            priority=PRIORITY,
            conditions=[elbv2.ListenerCondition.path_patterns([PATH_PATTERN])],
            action=elbv2.ListenerAction.forward([self.target_group]),
        )
