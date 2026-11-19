"""Grafana: a Fargate service behind the public load balancer's sign-in (system design, sections
7.1 and 7.4).

**The task.** One task of 0.25 vCPU and 1 GB on arm64, about the $9 a month of section 11.2, in
the VPC's private subnets with no public address, in Grafana's security group of
`rhi.network.security`, which admits the public load balancer on port 3000 and nothing else. The
image is `services/grafana/`: the pinned upstream image with its settings, its two data sources
and its dashboards. Grafana keeps no state outside the task: a user is created again from the
identity header, and the dashboards are files of the image.

**The dashboards are built at synthesis, from the configuration that is being deployed**
(sections 4.7 and 7.4). In the repository the image's dashboards folder holds none: the pools are
part of the real configuration, which is never committed. So the image's build context is not
`services/grafana/` itself but a directory `image_context` assembles: the files of that folder,
and, in its dashboards folder, every dashboard `rhi_dashboards.build` writes from the
configuration the construct is given, with a row for each of its pools. Nothing is written into
the repository, and no container is started: the pipeline's asset step builds the image from the
context the cloud assembly holds. The build gives the same bytes for the same configuration, and
the asset's hash covers the context's files and nothing of the run, so Grafana is redeployed when
the image's folder, a dashboard's module or the configuration's pools change, and not otherwise.
A dashboard reads an engine's metrics by the names of the engine's row in the per-engine table
(`rhi_common.engine_metrics`), so a pool whose engine had no row would fail synthesis here, with
the engine's name, and nothing would be deployed.

**How it is reached.** One rule of the public load balancer's HTTPS listener, at priority
`PRIORITY` (the gateway's is at 10 and the issuer's at 20), for the paths under `/grafana/`. Its
first action authenticates against the admin's user pool (`rhi.access.sign_in`), and only its
second forwards to the service's target group, on port 3000: a request that has not signed in is
sent to the sign-in and never reaches Grafana. Grafana has no login of its own and trusts the
identity header the load balancer sets, so a rule that forwarded here without authenticating
first would let anyone name themselves: this is the only rule that forwards to this target group,
and `tests/policy/test_admin_surfaces.py` holds every template to it.

The rule also takes `CALLBACK_PATH`, where the sign-in sends the browser back: the load balancer
answers that path itself, from the rule that authenticates, and sends the browser on to the page
it first asked for. Nothing under that path is forwarded without the same authentication.

**Only when the listener exists.** The listener exists only while the configuration's
`https_listener` is true (`rhi.network.edge`), and ECS refuses a service whose target group
belongs to no load balancer: without the listener the service has no target group, no rule and no
load balancer, and nothing reaches it from the internet.

**What Grafana is told**, as the environment variables the image reads (`services/grafana/`):

- `PROMETHEUS_URL`: the workspace's query address, which CloudFormation gives at deployment;
- `AWS_REGION`: the region of the stack, for both data sources;
- `GF_SERVER_DOMAIN`: the configuration's domain, under which Grafana is served at `/grafana/`.

No template holds a credential: both data sources sign their requests with the task's role.

**The task's role** is trusted by ECS tasks and holds no AWS managed policy. What it may do, each
with its reason, and nothing else:

- `prometheus.QUERY_ACTIONS` on the one workspace, by its ARN: the dashboards' queries;
- `LOG_ACTIONS` on the log groups of the gateway, the scaler and the issuer, `/rhi/gateway`,
  `/rhi/scaler` and `/rhi/issuer`, and on that of each pool of the configuration,
  `/rhi/pools/<pool>`: the dashboards' log queries. Each is named by its fixed name, as text, and
  never by a reference to the stack that creates it: the pools stack and the gateway stack depend
  on this one, which must not depend on them. Each ARN ends `:*`, which is how CloudWatch Logs
  names a log group and its streams in a policy. No other log group is named, and none by a
  pattern: not a build project's, whose logs hold the real configuration, not Grafana's own, and
  not that of a pool the configuration does not hold;
- `EVERY_RESOURCE_ACTIONS` on every resource, the only way CloudWatch grants them: reading
  metrics (the load balancers', Auto Scaling's, the pipeline's and the account's month-to-date
  charge) and fetching the results of a log query. Grafana can start a query only on a log
  group the role names, and fetching results takes the identifier of a query, which Grafana
  holds for the queries it started.

Listing the account's log groups, stopping a query and reading alarms are left out: a dashboard
whose panels name their log groups and their metrics is shown without them.

**Each pool's log group brings a cdk-nag finding of its own.** cdk-nag raises a finding for an ARN
that ends `:*`, and the finding's id holds the ARN, so it holds the pool's name.
`tests/policy/acknowledged/observability_grafana.toml` lists it once, with `<pool>` in place of
the name, and `rhi.nag` acknowledges it for each pool of the configuration and for no other name.

**A deployment that cannot start its task fails**: the service's circuit breaker ends it and
returns the service to the task it had.

**The logs** of the container go to the log group `LOG_GROUP_NAME`, for 90 days.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_actions as elbv2_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from rhi import packaging
from rhi.access import issuer
from rhi.access.sign_in import CALLBACK_PATH
from rhi.gateway import service as gateway
from rhi.network.security import GRAFANA_PORT
from rhi.observability import prometheus
from rhi.pools import scaler
from rhi.pools.role import log_group_name
from rhi_dashboards import build as dashboards

if TYPE_CHECKING:
    from rhi.access.sign_in import SignIn
    from rhi.config import Config
    from rhi.observability.prometheus import PrometheusWorkspace

# 0.25 vCPU, in ECS's CPU units, and 1 GB (section 11.2: about $9 a month on arm64).
CPU_UNITS = 256
MEMORY_MIB = 1024
CPU_ARCHITECTURE = ecs.CpuArchitecture.ARM64
IMAGE_PLATFORM = ecr_assets.Platform.LINUX_ARM64
# The image's folder: its Dockerfile, its settings, its data sources and an empty dashboards folder.
IMAGE_FOLDER = packaging.REPO / "services" / "grafana"
# The folder of the build context that the Dockerfile copies the dashboards from.
DASHBOARDS_FOLDER = "dashboards"

LOG_GROUP_NAME = "/rhi/grafana"
LOG_RETENTION = logs.RetentionDays.THREE_MONTHS

# The variables the image reads (`services/grafana/grafana.ini` and its data sources).
PROMETHEUS_URL_VARIABLE = "PROMETHEUS_URL"
REGION_VARIABLE = "AWS_REGION"
DOMAIN_VARIABLE = "GF_SERVER_DOMAIN"

CONTAINER = "Grafana"
# The listener's rules need distinct priorities; the design gives none (gateway 10, issuer 20).
PRIORITY = 30
# Grafana is served under this prefix of the endpoint's domain (`services/grafana/grafana.ini`).
PREFIX = "/grafana/"
PATH_PATTERN = PREFIX + "*"
# Grafana's own health route, which answers without a sign-in and reads no dashboard.
HEALTH_CHECK_PATH = PREFIX + "api/health"
# How long a new task has to answer its health check: Grafana keeps no state, so every start
# builds its database first.
START_SECONDS = 300
# Grafana holds no request for long: a deregistering task keeps its requests half a minute.
DEREGISTRATION_SECONDS = 30
# What the load balancer asks of the user pool: who signed in, and nothing else.
SCOPE = "openid"

# The log groups of the services, by their fixed names. Those of the pools' instances are added
# for the pools of the configuration, each by its own name (`readable_log_groups`).
SERVICE_LOG_GROUPS = (gateway.LOG_GROUP_NAME, scaler.LOG_GROUP_NAME, issuer.LOG_GROUP_NAME)
# What a log query of the CloudWatch data source takes on a log group: starting the query, the
# fields the query editor lists, and the events around a line.
LOG_ACTIONS = ("logs:StartQuery", "logs:GetLogGroupFields", "logs:GetLogEvents")
# What CloudWatch grants on every resource only: reading metrics, listing them, and fetching the
# results of a log query. `tests/policy/acknowledged/observability_grafana.toml` lists each.
EVERY_RESOURCE_ACTIONS = ("cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "logs:GetQueryResults")


def readable_log_groups(config: Config) -> tuple[str, ...]:
    """The names of the log groups Grafana may query: the services', and that of each pool of the
    configuration, `/rhi/pools/<pool>`. No pattern: a pool the configuration does not hold has no
    log group here."""
    return (*SERVICE_LOG_GROUPS, *(log_group_name(pool.name) for pool in config.pools))


def image_context(config: Config, *, folder: Path = IMAGE_FOLDER) -> str:
    """A build context for Grafana's image, for the `directory` of a `DockerImageAsset`: the files
    of `folder`, and in its dashboards folder every dashboard built from the configuration, each
    as `<uid>.json`.

    The same folder and the same configuration give the same files, so the same asset hash. The
    directory is a temporary one, removed when the process ends: CDK copies it into the cloud
    assembly when the asset is created.
    """
    context = Path(tempfile.mkdtemp(prefix="rhi-grafana-image-")) / "context"
    atexit.register(shutil.rmtree, context.parent, ignore_errors=True)
    shutil.copytree(folder, context)
    dashboards.write(config, context / DASHBOARDS_FOLDER)
    return str(context)


class Grafana(Construct):
    """Grafana's cluster, as `cluster`, its task definition, as `task_definition`, the task's
    role, as `task_role`, the container's log group, as `log_group`, and the service, as
    `service`. When `listener` is given, the service's target group and its rule on that
    listener, as `target_group` and `rule`; both are None without one."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        vpc: ec2.IVpc,
        security_group: ec2.ISecurityGroup,
        workspace: PrometheusWorkspace,
        sign_in: SignIn,
        listener: elbv2.IApplicationListener | None,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)

        # ty reads the CDK's generated classes as unlike the protocols they implement where a method
        # names a parameter otherwise (`grantee` for `identity`): jsii passes it by position.
        ecs_tasks = iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        # Its own role, with no AWS managed policy: what Grafana may do is listed here, whole.
        self.task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=ecs_tasks,  # ty: ignore[invalid-argument-type]
            description="Grafana's task.",
        )
        self.task_role.add_to_policy(
            iam.PolicyStatement(actions=list(prometheus.QUERY_ACTIONS), resources=[workspace.workspace.attr_arn])
        )
        # Each log group by its name, as text, in this stack's partition, region and account: no
        # reference to the stack that creates it. `:*` is the log group and its streams.
        self.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=list(LOG_ACTIONS),
                resources=[
                    stack.format_arn(
                        service="logs",
                        resource="log-group",
                        resource_name=f"{name}:*",
                        arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
                    )
                    for name in readable_log_groups(config)
                ],
            )
        )
        self.task_role.add_to_policy(iam.PolicyStatement(actions=list(EVERY_RESOURCE_ACTIONS), resources=["*"]))

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=LOG_GROUP_NAME,
            retention=LOG_RETENTION,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # Grafana shows the metrics; its own are not collected: Container Insights stays off.
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
        self.task_definition.add_container(
            CONTAINER,
            image=ecs.ContainerImage.from_asset(image_context(config), platform=IMAGE_PLATFORM),
            environment={
                PROMETHEUS_URL_VARIABLE: workspace.workspace.attr_prometheus_endpoint,
                REGION_VARIABLE: stack.region,
                DOMAIN_VARIABLE: config.domain,
            },
            port_mappings=[ecs.PortMapping(container_port=GRAFANA_PORT)],
            logging=ecs.LogDrivers.aws_logs(log_group=self.log_group, stream_prefix="grafana"),
        )

        self.service = ecs.FargateService(
            self,
            "Service",
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=1,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            assign_public_ip=False,
            security_groups=[security_group],
            # A deployment starts the new task, then stops the old one.
            min_healthy_percent=100,
            max_healthy_percent=200,
            # A deployment whose task does not start fails, and the service goes back to the task
            # it had: without this CloudFormation waits three hours for a steady state.
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            # Behind the load balancer only: how long ECS leaves a new task before the target
            # group's health check may stop it. Grafana builds its database at every start.
            health_check_grace_period=cdk.Duration.seconds(START_SECONDS) if listener is not None else None,
        )

        self.target_group: elbv2.ApplicationTargetGroup | None = None
        self.rule: elbv2.ApplicationListenerRule | None = None
        if listener is None:
            return
        self.target_group = elbv2.ApplicationTargetGroup(
            self,
            "Target",
            vpc=vpc,
            port=GRAFANA_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            # The task by its address: ECS registers it, and deregisters it before it stops it.
            targets=[self.service.load_balancer_target(container_name=CONTAINER, container_port=GRAFANA_PORT)],
            deregistration_delay=cdk.Duration.seconds(DEREGISTRATION_SECONDS),
            health_check=elbv2.HealthCheck(
                path=HEALTH_CHECK_PATH,
                protocol=elbv2.Protocol.HTTP,
                healthy_http_codes="200",
            ),
        )
        # The sign-in first, then the forward: the CDK orders the actions as they are chained. A
        # request that is not signed in is sent to the sign-in, which is the action's default,
        # stated here. The public load balancer's security group lets it reach the user pool's
        # endpoints already (`rhi.network.security`), so the action is told to add no rule to it.
        authenticated = elbv2_actions.AuthenticateCognitoAction(
            user_pool=sign_in.user_pool,
            user_pool_client=sign_in.client,
            user_pool_domain=sign_in.domain,
            scope=SCOPE,
            on_unauthenticated_request=elbv2.UnauthenticatedAction.AUTHENTICATE,
            allow_https_outbound=False,
            next=elbv2.ListenerAction.forward([self.target_group]),
        )
        self.rule = elbv2.ApplicationListenerRule(
            self,
            "Rule",
            listener=listener,
            priority=PRIORITY,
            conditions=[elbv2.ListenerCondition.path_patterns([PATH_PATTERN, CALLBACK_PATH])],
            action=authenticated,
        )
