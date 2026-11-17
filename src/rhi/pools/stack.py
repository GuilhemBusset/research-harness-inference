"""The pools stack: the pools' state and heartbeats, the internal load balancer, the groups and the
scaler that sizes them (system design, sections 2, 4.2 and 5).

The stack may reference other stacks of the stage (the network, the workspace); none of them may
reference it back. The stack and the logical IDs of its tables are recorded in
`tests/policy/stateful_logical_ids.json`: moving a table to another stack replaces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import custom_resources as cr

from rhi.pools.group import PoolGroup
from rhi.pools.routing import Routing
from rhi.pools.scaler import Scaler
from rhi.pools.tables import PoolTables

if TYPE_CHECKING:
    from constructs import Construct

    from rhi.config import Config
    from rhi.network.stack import NetworkStack
    from rhi.observability.stack import ObservabilityStack


class PoolsStack(cdk.Stack):
    """Holds the pool-state and heartbeat tables, which the stack's later constructs take from
    `tables` (`tables.pool_state`, `tables.heartbeats`), the internal load balancer with one
    target group per pool, which they take from `routing` (`routing.target_groups[name]`), and each
    pool's launch template and Auto Scaling group, from `groups[name]`, and the scaler, from
    `scaler`, whose `scaler.function` another construct of the stage may allow its role to invoke."""

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
        self.tables = PoolTables(self, "Tables")
        # With the pools table empty, the listener and its default 404, and no target group.
        self.routing = Routing(
            self,
            "Routing",
            vpc=network.vpc,
            security_group=network.security_groups.internal_load_balancer,
            pools=config.pools,
        )
        # One launch template and group per pool, by the pool's name; none with the table empty.
        self.groups = {
            pool.name: PoolGroup(
                self,
                f"Pool-{pool.name}",
                pool=pool,
                vpc=network.vpc,
                security_group=network.security_groups.instances,
                target_group=self.routing.target_groups[pool.name],
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
        )
        # The groups' custom resources run on one function the CDK puts at the stack's root. That
        # function names its role's ARN, which orders the two already; its `DependsOn` on the role
        # repeats it, and cfn-lint warns (W3005).
        provider = self.node.try_find_child("AWS" + cr.AwsCustomResource.PROVIDER_FUNCTION_UUID.replace("-", ""))
        if provider is not None:
            function = provider.node.find_child("Resource")
            assert isinstance(function, cdk.CfnResource)
            function.add_deletion_override("DependsOn")
