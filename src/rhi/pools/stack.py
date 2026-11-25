"""The pools stack: the pools' state and heartbeats, the model bucket, the internal load balancer,
the groups, the scaler that sizes them, the releases their instances run and the lifecycle hooks
their instances complete (system design, sections 2, 4.2, 4.3, 4.4 and 5).

The stack may reference other stacks of the stage (the network, the workspace); none of them may
reference it back. The stack and the logical IDs of its tables and of its bucket are recorded in
`tests/policy/stateful_logical_ids.json`: moving a table to another stack replaces it.

The stack declares two outputs, which the pipeline's verification takes as inputs
(`rhi.pipeline.verify`):

- `ReleaseParameters`: each pool's name with the name of its release parameter. It is written as
  `release_parameters_text` writes it, without a quote: CodePipeline puts an output's value into
  the JSON of an action's configuration as it is.
- `HeartbeatsTable`: the name of the heartbeats table, which CloudFormation generates. The
  verification reads there the last lifecycle stage each instance reached.

**Parked** (the configuration's `parked`, sections 5.6 and 11.2), the stack holds no internal load
balancer, no group, warm pool or lifecycle hook, no release and no scaler or its schedule: nothing
runs. The tables, the model bucket, each pool's instance role with its log group, each pool's
secret and the scaler's log group, whose name is fixed (`rhi.pools.scaler.log_group`), stay, and
`ReleaseParameters` reads `NO_POOL`. The stack still deploys after the network stack and the
observability stack, whose exports it imports: the model bucket's policy names the S3 endpoint, and
the instance roles the workspace. So every export of those two stacks that it imports unparked, the
workspace's that only the scaler names with the pools table empty included (`_keep_exports`), is
kept, through the exporting stack's `export_value`: an export that a deployed stack imports cannot
be deleted, and this stack's update, which drops the import, comes after theirs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import custom_resources as cr
from constructs import Construct

from rhi.network.vpc import PRIVATE, ZONES, private_subnets
from rhi.pools import image
from rhi.pools.group import PoolGroup, release_parameter
from rhi.pools.hooks import PoolHooks
from rhi.pools.model_store import ModelStore
from rhi.pools.release import InstanceRefresher, PoolRelease, ReleaseFiles
from rhi.pools.role import PoolRole
from rhi.pools.routing import Routing
from rhi.pools.scaler import Scaler, log_group
from rhi.pools.secret import PoolSecret
from rhi.pools.tables import PoolTables

if TYPE_CHECKING:
    from collections.abc import Mapping

    from rhi.config import Config
    from rhi.network.stack import NetworkStack
    from rhi.observability.stack import ObservabilityStack
    from rhi_common.pool_config import Pool

# What separates two pools in the output of the release parameters, and a pool's name from its
# parameter's. Neither is a character of a pool's name or of a parameter's.
POOL_SEPARATOR = ","
NAME_SEPARATOR = "="
# What the output reads for no pool: an output has a value.
NO_POOL = "none"


def release_parameters_text(parameters: Mapping[str, str]) -> str:
    """Each pool's name and the name of its release parameter, as one text:
    `mock=/rhi/pools/mock/release` for one pool, a comma between two, and `NO_POOL` for no pool."""
    return POOL_SEPARATOR.join(f"{pool}{NAME_SEPARATOR}{name}" for pool, name in parameters.items()) or NO_POOL


def zone_positions(pool: Pool, zones: tuple[str, ...] | None) -> list[int]:
    """The positions, among the VPC's zones, of the zones the pool's group spans: those of the
    pool's own zones in the configuration's `zones`, which names the VPC's zones in the order of its
    subnets, or the first three for a pool that lists none. The configuration loader holds a pool's
    zones to that list, and refuses a pool that gives zones where the list is left out
    (`rhi.config`)."""
    if pool.zones is None:
        return list(range(ZONES))
    if zones is None:
        raise ValueError(f"the pool {pool.name!r} gives zones, and the configuration lists none")
    return [zones.index(zone) for zone in pool.zones]


class PoolsStack(cdk.Stack):
    """Holds the pool-state and heartbeat tables, which the stack's later constructs take from
    `tables` (`tables.pool_state`, `tables.heartbeats`), the model bucket, from `model_store`
    (`model_store.bucket`), which each GPU pool's instance role reads under its own pool's prefix,
    the internal load balancer with one target group per pool, which they take from `routing`
    (`routing.target_groups[name]`), each
    pool's instance role and log group, from `roles[name]` (`roles[name].role`, which later issues
    grant what their resources need), each pool's bearer secret, from `secrets[name]`
    (`secrets[name].secret`, whose `secrets[name].grant_read(grantee)` lets another construct of the
    stage, the gateway's, allow its role to read it), each pool's launch template and Auto Scaling group, from
    `groups[name]`, the scaler, from
    `scaler`, whose `scaler.function` another construct of the stage may allow its role to invoke,
    each pool's release, from `releases[name]`: its engine image, its release parameter and the
    instance refresh that a change to the release starts, each pool's lifecycle hooks, from
    `hooks[name]` (`hooks[name].launch`, `hooks[name].termination`), and the name of each pool's
    release parameter, from `release_parameters[name]` and, all of them, as the output
    `release_parameters_output`. The heartbeat table's name is the output `heartbeats_table_output`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        network: NetworkStack,
        observability: ObservabilityStack,
    ) -> None:
        super().__init__(scope, construct_id)
        # The VPC, its subnets and the internal load balancer's group are the network stack's: this
        # stack imports them through exports, the CDK's default, stated here. Network cannot drop an
        # export while this stack imports it.
        cdk.CrossStackReferences.of(self).consume(cdk.ReferenceStrength.STRONG)
        # The internal load balancer's access logs go to the network stack's bucket, whose policy
        # must let Elastic Load Balancing write before the load balancer's attributes are set: this
        # stack deploys after the network stack, whatever it imports from it.
        self.add_stack_dependency(
            network, "The internal load balancer logs to a bucket whose policy is in the network stack."
        )
        if config.parked:
            self._keep_exports(network, observability)
        self.tables = PoolTables(self, "Tables")
        # The heartbeat table's name, which the deployment generates, for the pipeline's verification.
        self.heartbeats_table_output = cdk.CfnOutput(
            self,
            "HeartbeatsTable",
            description="The name of the heartbeats table.",
            value=self.tables.heartbeats.table_name,
        )
        # The pools' weights, read through the network's S3 gateway endpoint and no other way. The
        # stage holds the one bucket whatever its pools: a pool's weights outlive the pool.
        self.model_store = ModelStore(self, "ModelStore", endpoint=network.s3_endpoint)
        # One instance role per pool, with its log group, by the pool's name. A GPU pool's role is
        # under the GPU pools' path and reads its own pool's weights; the mock pool has none.
        self.roles = {
            pool.name: PoolRole(
                self,
                f"Role-{pool.name}",
                pool=pool.name,
                gpu=image.is_gpu_pool(pool),
                heartbeats=self.tables.heartbeats,
                workspace=observability.prometheus,
            )
            for pool in config.pools
        }
        # One bearer secret per pool, by the pool's name, readable by that pool's instance role.
        self.secrets = {
            pool.name: PoolSecret(self, f"Secret-{pool.name}", pool=pool.name, role=self.roles[pool.name].role)
            for pool in config.pools
        }
        self.routing: Routing | None = None
        self.groups: dict[str, PoolGroup] = {}
        self.scaler: Scaler | None = None
        self.releases: dict[str, PoolRelease] = {}
        self.hooks: dict[str, PoolHooks] = {}
        if config.parked:
            # The scaler's log group alone, at the scaler's path.
            log_group(Construct(self, "Scaler"))
        else:
            self._fleet(config, network, observability)
        # The name of each pool's release parameter, by the pool's name, as text: a stack that does
        # not hold the parameter, the pipeline's, names it so. The output holds the same names.
        self.release_parameters = {name: release_parameter(name) for name in self.releases}
        self.release_parameters_output = cdk.CfnOutput(
            self,
            "ReleaseParameters",
            description="Each pool's name and the name of its release parameter.",
            value=release_parameters_text(self.release_parameters),
        )
        # The groups' and the releases' AWS SDK custom resources run on one function the CDK puts at the stack's root. That
        # function names its role's ARN, which orders the two already; its `DependsOn` on the role
        # repeats it, and cfn-lint warns (W3005).
        provider = self.node.try_find_child("AWS" + cr.AwsCustomResource.PROVIDER_FUNCTION_UUID.replace("-", ""))
        if provider is not None:
            function = provider.node.find_child("Resource")
            assert isinstance(function, cdk.CfnResource)
            function.add_deletion_override("DependsOn")

    def _fleet(self, config: Config, network: NetworkStack, observability: ObservabilityStack) -> None:
        """What runs: the internal load balancer, the groups, the scaler, the releases and the hooks."""
        # With the pools table empty, the listener and its default 404, and no target group.
        self.routing = Routing(
            self,
            "Routing",
            vpc=network.vpc,
            security_group=network.security_groups.internal_load_balancer,
            access_logs_bucket=network.access_logs.bucket,
            pools=config.pools,
        )
        # One launch template and group per pool, by the pool's name; none with the table empty.
        # A group spans the private subnets of its pool's zones alone, where the load balancer
        # above spans every private subnet: a target in a zone the load balancer does not span
        # receives no traffic from it.
        self.groups = {
            pool.name: PoolGroup(
                self,
                f"Pool-{pool.name}",
                pool=pool,
                subnets=private_subnets(network.vpc, zone_positions(pool, config.zones)),
                security_group=network.security_groups.instances,
                target_group=self.routing.target_groups[pool.name],
                role=self.roles[pool.name].role,
                nightly_stop=config.nightly_stop,
                time_zone=config.time_zone,
            )
            for pool in config.pools
        }
        # The one writer of the groups' desired capacity, which writes its metrics to the workspace.
        self.scaler = Scaler(
            self,
            "Scaler",
            pools=config.pools,
            groups=self.groups,
            target_groups=self.routing.target_groups,
            heartbeats=self.tables.heartbeats,
            pool_state=self.tables.pool_state,
            workspace=observability.prometheus,
            session_max_hours=config.session_max_hours,
        )
        # One release per pool, by the pool's name: its engine image, the parameter its instances
        # read at every start, and the refresh of its group when the release changes. The pools
        # share the release's files and what runs the refreshes; with the table empty there is none.
        if config.pools:
            files = ReleaseFiles(self, "ReleaseFiles")
            refresher = InstanceRefresher(self, "InstanceRefresher", pools=config.pools)
            self.releases = {
                pool.name: PoolRelease(
                    self,
                    f"Release-{pool.name}",
                    pool=pool,
                    files=files,
                    refresher=refresher,
                    group=self.groups[pool.name],
                    role=self.roles[pool.name].role,
                    heartbeats=self.tables.heartbeats,
                    workspace=observability.prometheus,
                )
                for pool in config.pools
            }
        # One launch hook and one termination hook per pool, by the pool's name. They come last among
        # a pool's pieces: with them, an instance enters service only when its agent completes the
        # launch hook, which takes the role, the secret and the release above.
        self.hooks = {
            pool.name: PoolHooks(self, f"Hooks-{pool.name}", pool=pool, group=self.groups[pool.name].group)
            for pool in config.pools
        }

    def _keep_exports(self, network: NetworkStack, observability: ObservabilityStack) -> None:
        """Parked, keeps the exports this stack imports unparked of what stays in the network stack
        and the observability stack (the module's docstring)."""
        for value in (
            network.vpc.vpc_id,
            *network.vpc.select_subnets(subnet_group_name=PRIVATE).subnet_ids,
            network.security_groups.internal_load_balancer.security_group_id,
            network.security_groups.instances.security_group_id,
            network.access_logs.bucket.bucket_name,
        ):
            network.export_value(value)
        # The workspace's endpoint and ARN, which the instance roles and the scaler name: with the pools
        # table empty, the scaler alone imports them unparked.
        workspace = observability.prometheus.workspace
        for value in (workspace.attr_prometheus_endpoint, workspace.attr_arn):
            observability.export_value(value)
