"""The gateway's service: an always-on Fargate service, its tasks' environment, their role and the
collector beside the gateway (system design, sections 2, 5.3, 6.2, 6.6 and 11.2).

**The tasks.** As many as the configuration's `gateway_tasks`, each of 0.5 vCPU and 1 GB on arm64,
the architecture the idle cost of section 11.2 is priced for, in the VPC's private subnets with no
public address, in the gateway's security group of `rhi.network.security`. The gateway listens on
port 8080. A deployment starts new tasks before it stops old ones.

**How it is reached.** One rule of the public load balancer's HTTPS listener, at priority
`PRIORITY`, forwards the paths of the gateway's route table (`PATH_PATTERNS`: everything under
`/v1/`, and `/status` and `/api/hello`) to the service's target group, on port 8080. The
issuer's rule is at 20 and Grafana's at 30; the load balancer answers every other path with its
fixed 404, and the gateway's own table is still the control. The listener exists only while the
configuration's `https_listener` is true (`rhi.network.edge`), and ECS refuses a service whose
target group belongs to no load balancer: without the listener the service has no target group,
no rule and no load balancer, and nothing reaches it from the internet. The security group of
`rhi.network.security` already admits the load balancer on port 8080, so the attachment adds no
rule of its own.

**A deployment never cuts a request.** ECS waits for an old task's target to finish deregistering
before it stops the task, and the target group's deregistration delay is `DEREGISTRATION_SECONDS`,
1800: a held request or a stream up to 30 minutes long finishes, and a gateway deployment takes
that long to complete (section 8.5; the number is a default of section 17).

**The health check** sends `GET /v1/models`, as a load balancer does, with no key, and counts the
status 401 as healthy. The route table is closed, and its one route answered without a key,
`HEAD /api/hello`, is not a GET; the gateway answers a request that has no key from its headers
alone, with no lookup, so the check costs nothing and adds no route.

**The image** is built from the context `rhi.packaging.image_context` gives the gateway:
`rhi_common`, `rhi_gateway`, the gateway's Dockerfile and the gateway group's requirements with
their hashes. The pipeline's asset step builds it for linux/arm64.

**What the gateway is told**, as the environment variables `rhi_gateway.settings` reads:

- `RHI_POOLS`: the pools as the stage loaded them, without the zones of their groups
  (`rhi.config.deployed_entry`), `{}` with none;
- `RHI_DEFAULT_POOL`: the configuration's default pool, left out when it has none;
- `RHI_UPSTREAM_URL`: the internal load balancer's listener, by the load balancer's DNS name;
- `RHI_TARGET_GROUPS` and `RHI_POOL_SECRETS`: each pool's target group and each pool's secret, by
  ARN. No variable, parameter or template holds a secret's value: the gateway reads it from
  Secrets Manager at start, with the task's role;
- `RHI_KEYS_TABLE`, `RHI_USAGE_TABLE`, `RHI_HEARTBEAT_TABLE`, `RHI_POOL_STATE_TABLE`: the names the
  deployment gives the four tables;
- `RHI_SCALER_FUNCTION`: the scaler function's name;
- `RHI_COURSE_END`: the configuration's course end date, YYYY-MM-DD, by which every usage row
  the gateway writes expires (section 6.6);
- the seven numeric variables, each from its configuration key (`NUMERIC`): the last,
  `RHI_SESSION_MAX_HOURS`, is the longest a session may last, under which the gateway reads every
  pool's session (section 5.6);
- the three floors of the objectives, each from its configuration key (`FLOORS`), which the
  gateway's histograms take among their boundaries.

With no pool the environment is still one the gateway starts with, so the service reaches a
steady state on a configuration whose pools table is empty.

**The collector** is the task's second container: the OpenTelemetry collector's contrib
distribution, release 0.165.0, named by the digest of its multi-platform index (`COLLECTOR_IMAGE`;
an update is a change to that line). It runs `services/gateway/collector/config.yaml`, which that
release validates and which the task gives it whole in `RHI_COLLECTOR_CONFIG`: the collector reads
its configuration from that variable (`--config=env:`), so no second image is built to carry one
file. The same environment gives it `RHI_WORKSPACE_URL`, the workspace's remote-write address, and
`RHI_REGION`. It scrapes the gateway on the loopback address, port 9091, which the two containers
of a task share, and signs its writes with the task's role. It is not essential: a collector that
stops does not stop the gateway beside it, and the container's restart policy has ECS start again
one that had been running.

**A deployment that cannot start its tasks fails**: the service's circuit breaker ends it and
returns the service to the tasks it had, where CloudFormation would otherwise wait three hours for
a steady state.

**The logs** of both containers go to the log group `LOG_GROUP_NAME`, a fixed name, so that
Grafana's role can name it without a reference to this stack. It keeps 90 days.

**The task's role** is trusted by ECS tasks and holds no AWS managed policy. What it may do, each
with its reason, and nothing else:

- read the items of the keys table (a request's key) and of the pool-state table (each pool's
  state record and the scaler's note), by their ARNs;
- read and write the items of the usage table (the students' budgets) and of the heartbeat table
  (this task's rows), by their ARNs;
- `secretsmanager:GetSecretValue` and `DescribeSecret` on each pool's bearer secret, by its fixed
  name (`rhi.pools.secret`): the gateway sends it to the pool's engines;
- `elasticloadbalancing:DescribeTargetHealth` on every resource, the only way AWS grants it: the
  gateway reads each pool's target group to know when the pool is ready;
- `lambda:InvokeFunction` on the scaler function alone: a request for a pool that sleeps wakes it;
- `aps:RemoteWrite` on the metrics workspace alone: the collector pushes the gateway's metrics.

Pulling the two images' layers and writing the two containers' logs is the task's execution role,
which ECS uses to start a task and no container holds: the image's layers from the bootstrap's
image repository, `ecr:GetAuthorizationToken` on every resource, the only way AWS grants it, and
the streams of the log group above.

**The owner's part.** ECS runs a service through its service-linked role, which a deployment
cannot create (`setup/bootstrap/README.md`). This is the first construct that uses ECS, so before
it deploys the owner creates that role once, unless the account already has it:
`aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from rhi import packaging
from rhi.access.issuer import READ_ACTIONS, WRITE_ACTIONS
from rhi.config import deployed_entry
from rhi.network.security import GATEWAY_PORT, INTERNAL_LOAD_BALANCER_PORT
from rhi.observability import prometheus
from rhi.pools.scaler import REMOTE_WRITE_PATH

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_lambda as lambda_

    from rhi.config import Config
    from rhi.observability.prometheus import PrometheusWorkspace
    from rhi.pools.secret import PoolSecret

# 0.5 vCPU, in ECS's CPU units, and 1 GB (sections 2 and 11.2).
CPU_UNITS = 512
MEMORY_MIB = 1024
# Graviton: what section 11.2 prices the two tasks at, and the architecture of every function.
CPU_ARCHITECTURE = ecs.CpuArchitecture.ARM64
IMAGE_PLATFORM = ecr_assets.Platform.LINUX_ARM64
DOCKERFILE = packaging.REPO / "services" / "gateway" / "Dockerfile"

LOG_GROUP_NAME = "/rhi/gateway"
LOG_RETENTION = logs.RetentionDays.THREE_MONTHS

# The collector: the contrib distribution of the OpenTelemetry collector, release 0.165.0, the one
# that validates the configuration below, by the digest of its index, which holds linux/arm64.
COLLECTOR_IMAGE = (
    "otel/opentelemetry-collector-contrib:0.165.0"
    "@sha256:e40d61f10c9a617cca1ffd1be4d7e2b3a4b0f0001798ff07e58cabe82e618cea"
)
COLLECTOR_CONFIG = packaging.REPO / "services" / "gateway" / "collector" / "config.yaml"
# The variable the collector reads its configuration from, and the two that configuration reads.
COLLECTOR_CONFIG_VARIABLE = "RHI_COLLECTOR_CONFIG"
WORKSPACE_URL_VARIABLE = "RHI_WORKSPACE_URL"
REGION_VARIABLE = "RHI_REGION"

# The variables `rhi_gateway.settings` reads.
POOLS_VARIABLE = "RHI_POOLS"
DEFAULT_POOL_VARIABLE = "RHI_DEFAULT_POOL"
UPSTREAM_URL_VARIABLE = "RHI_UPSTREAM_URL"
TARGET_GROUPS_VARIABLE = "RHI_TARGET_GROUPS"
POOL_SECRETS_VARIABLE = "RHI_POOL_SECRETS"
KEYS_TABLE_VARIABLE = "RHI_KEYS_TABLE"
USAGE_TABLE_VARIABLE = "RHI_USAGE_TABLE"
HEARTBEAT_TABLE_VARIABLE = "RHI_HEARTBEAT_TABLE"
POOL_STATE_TABLE_VARIABLE = "RHI_POOL_STATE_TABLE"
SCALER_FUNCTION_VARIABLE = "RHI_SCALER_FUNCTION"
COURSE_END_VARIABLE = "RHI_COURSE_END"
# Each numeric variable, and the configuration key it is written from.
NUMERIC = {
    "RHI_BODY_LIMIT_BYTES": "request_body_limit_bytes",
    "RHI_LONGEST_REQUEST_SECONDS": "longest_request_seconds",
    "RHI_KEEPALIVE_SECONDS": "keepalive_seconds",
    "RHI_RETRY_AFTER_WARMING": "retry_after_warming_seconds",
    "RHI_RETRY_AFTER_CAPACITY_UNAVAILABLE": "retry_after_capacity_unavailable_seconds",
    "RHI_RETRY_AFTER_CONCURRENCY_LIMIT": "retry_after_concurrency_limit_seconds",
    "RHI_SESSION_MAX_HOURS": "session_max_hours",
}
# Each floor of the objectives, and the configuration key it is written from: the gateway takes
# it among its histograms' boundaries (`rhi_gateway.settings`).
FLOORS = {
    "RHI_TARGET_TTFT_P95_SECONDS": "target_ttft_p95_seconds",
    "RHI_TARGET_STREAM_MEDIAN_TOKENS_PER_SECOND": "target_stream_median_tokens_per_second",
    "RHI_TARGET_STREAM_P10_TOKENS_PER_SECOND": "target_stream_p10_tokens_per_second",
}

# The container that listens, by the name the task definition gives it.
GATEWAY_CONTAINER = "Gateway"
# The listener's rules need distinct priorities; the design gives none (issuer 20, Grafana 30).
PRIORITY = 10
# The paths of the gateway's route table (`rhi_gateway.routes`): the forwarded and local routes
# under /v1/, and the three others.
PATH_PATTERNS = ["/v1/*", "/status", "/api/hello"]
# How long a deregistering task keeps its requests: the longest request, 30 minutes.
DEREGISTRATION_SECONDS = 1800
# What the health check asks, with no key, and the status the gateway answers it with.
HEALTH_CHECK_PATH = "/v1/models"
HEALTH_CHECK_STATUS = "401"

TARGET_HEALTH_ACTIONS = ["elasticloadbalancing:DescribeTargetHealth"]
INVOKE_ACTIONS = ["lambda:InvokeFunction"]


def log_group(scope: Construct) -> logs.LogGroup:
    """The gateway's log group, of a fixed name, as the child `Logs` of `scope`: the construct's own, or,
    for a parked stage, a construct of the same ID that holds the log group alone, so that it keeps
    its logical ID while what writes to it is gone (system design, section 5.6)."""
    return logs.LogGroup(
        scope,
        "Logs",
        log_group_name=LOG_GROUP_NAME,
        retention=LOG_RETENTION,
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )


class GatewayService(Construct):
    """The gateway's cluster, as `cluster`, its task definition, as `task_definition`, the task's
    role, as `task_role`, the two containers' log group, as `log_group`, and the service, as
    `service`. When `listener` is given, the service's target group and its rule on that listener,
    as `target_group` and `rule`; both are None without one."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        vpc: ec2.IVpc,
        security_group: ec2.ISecurityGroup,
        upstream: elbv2.IApplicationLoadBalancer,
        target_groups: Mapping[str, elbv2.IApplicationTargetGroup],
        secrets: Mapping[str, PoolSecret],
        keys: dynamodb.ITable,
        usage: dynamodb.ITable,
        heartbeats: dynamodb.ITable,
        pool_state: dynamodb.ITable,
        scaler: lambda_.Function,
        workspace: PrometheusWorkspace,
        listener: elbv2.IApplicationListener | None,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        names = [pool.name for pool in config.pools]

        # ty reads the CDK's generated classes as unlike the protocols they implement where a method
        # names a parameter otherwise (`grantee` for `identity`): jsii passes it by position.
        ecs_tasks = iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        # Its own role, with no AWS managed policy: what a task may do is listed here, whole.
        self.task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=ecs_tasks,  # ty: ignore[invalid-argument-type]
            description="The gateway's tasks.",
        )
        self.task_role.add_to_policy(
            iam.PolicyStatement(actions=READ_ACTIONS, resources=[keys.table_arn, pool_state.table_arn])
        )
        self.task_role.add_to_policy(
            iam.PolicyStatement(actions=READ_ACTIONS + WRITE_ACTIONS, resources=[usage.table_arn, heartbeats.table_arn])
        )
        for name in names:
            secrets[name].grant_read(self.task_role)
        self.task_role.add_to_policy(iam.PolicyStatement(actions=TARGET_HEALTH_ACTIONS, resources=["*"]))
        # The function itself, as the gateway invokes it: no version and no alias.
        self.task_role.add_to_policy(iam.PolicyStatement(actions=INVOKE_ACTIONS, resources=[scaler.function_arn]))
        # Inline rather than the workspace's `write_policy`: a policy of another stack is one the
        # invariants of this stack's template cannot read.
        self.task_role.add_to_policy(
            iam.PolicyStatement(actions=list(prometheus.WRITE_ACTIONS), resources=[workspace.workspace.attr_arn])
        )

        self.log_group = log_group(self)

        # The gateway's metrics go to the workspace: Container Insights would be a second store.
        self.cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.DISABLED)
        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "Task",
            cpu=CPU_UNITS,
            memory_limit_mib=MEMORY_MIB,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=CPU_ARCHITECTURE,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
            task_role=self.task_role,  # ty: ignore[invalid-argument-type] - as above
        )

        environment = {
            POOLS_VARIABLE: json.dumps({pool.name: deployed_entry(pool) for pool in config.pools}),
            UPSTREAM_URL_VARIABLE: f"http://{upstream.load_balancer_dns_name}:{INTERNAL_LOAD_BALANCER_PORT}",
            TARGET_GROUPS_VARIABLE: stack.to_json_string(
                {name: target_groups[name].target_group_arn for name in names}
            ),
            # The secret's ARN, never its value.
            POOL_SECRETS_VARIABLE: stack.to_json_string({name: secrets[name].secret.secret_arn for name in names}),
            KEYS_TABLE_VARIABLE: keys.table_name,
            USAGE_TABLE_VARIABLE: usage.table_name,
            HEARTBEAT_TABLE_VARIABLE: heartbeats.table_name,
            POOL_STATE_TABLE_VARIABLE: pool_state.table_name,
            SCALER_FUNCTION_VARIABLE: scaler.function_name,
            COURSE_END_VARIABLE: config.course_end.isoformat(),
            **{variable: str(getattr(config, key)) for variable, key in NUMERIC.items()},
            **{variable: _decimal(getattr(config, key)) for variable, key in FLOORS.items()},
        }
        if config.default_pool is not None:
            environment[DEFAULT_POOL_VARIABLE] = config.default_pool

        self.task_definition.add_container(
            GATEWAY_CONTAINER,
            image=ecs.ContainerImage.from_asset(
                packaging.image_context("gateway", DOCKERFILE),
                platform=IMAGE_PLATFORM,
            ),
            environment=environment,
            port_mappings=[ecs.PortMapping(container_port=GATEWAY_PORT)],
            logging=ecs.LogDrivers.aws_logs(log_group=self.log_group, stream_prefix="gateway"),
        )
        self.task_definition.add_container(
            "Collector",
            image=ecs.ContainerImage.from_registry(COLLECTOR_IMAGE),
            command=[f"--config=env:{COLLECTOR_CONFIG_VARIABLE}"],
            environment={
                COLLECTOR_CONFIG_VARIABLE: _as_it_stands(COLLECTOR_CONFIG.read_text(encoding="utf-8")),
                WORKSPACE_URL_VARIABLE: cdk.Fn.join(
                    "", [workspace.workspace.attr_prometheus_endpoint, REMOTE_WRITE_PATH]
                ),
                REGION_VARIABLE: stack.region,
            },
            # The gateway serves without its collector, and ECS starts again one that had been running.
            essential=False,
            enable_restart_policy=True,
            logging=ecs.LogDrivers.aws_logs(log_group=self.log_group, stream_prefix="collector"),
        )

        self.service = ecs.FargateService(
            self,
            "Service",
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=config.gateway_tasks,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            assign_public_ip=False,
            security_groups=[security_group],
            # A deployment starts the new tasks, then stops the old ones: with one task too.
            min_healthy_percent=100,
            max_healthy_percent=200,
            # A deployment whose tasks do not start fails, and the service goes back to the tasks
            # it had: without this CloudFormation waits three hours for a steady state.
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
        )

        self.target_group: elbv2.ApplicationTargetGroup | None = None
        self.rule: elbv2.ApplicationListenerRule | None = None
        if listener is None:
            return
        self.target_group = elbv2.ApplicationTargetGroup(
            self,
            "Target",
            vpc=vpc,
            port=GATEWAY_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            # Each task by its address: ECS registers it, and deregisters it before it stops it.
            targets=[self.service.load_balancer_target(container_name=GATEWAY_CONTAINER, container_port=GATEWAY_PORT)],
            deregistration_delay=cdk.Duration.seconds(DEREGISTRATION_SECONDS),
            # A load balancer's HTTP check is a GET that carries no header, so no key.
            health_check=elbv2.HealthCheck(
                path=HEALTH_CHECK_PATH,
                protocol=elbv2.Protocol.HTTP,
                healthy_http_codes=HEALTH_CHECK_STATUS,
            ),
        )
        self.rule = elbv2.ApplicationListenerRule(
            self,
            "Rule",
            listener=listener,
            priority=PRIORITY,
            conditions=[elbv2.ListenerCondition.path_patterns(PATH_PATTERNS)],
            action=elbv2.ListenerAction.forward([self.target_group]),
        )


def _decimal(value: float) -> str:
    """A floor in the digits the gateway reads: the fewest that give it back, with no exponent."""
    text = format(Decimal(repr(float(value))), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _as_it_stands(text: str) -> str:
    """A text that holds `${...}`, the collector's own syntax, as the substitution that gives
    CloudFormation the same text. cfn-lint reads `${...}` in a plain value as a parameter that
    lacks its `Fn::Sub` (E1029), and refuses an `Fn::Sub` that substitutes nothing (W1020): so
    the substitution writes each `$` that opens a `${`, and nothing else."""
    return cdk.Fn.sub(text.replace("${", "${Dollar}{"), {"Dollar": "$"})
