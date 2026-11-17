"""The pools stack: the pools' state and heartbeats, and later the internal load balancer, the
groups and the scaler that use them (system design, sections 2, 4.2 and 5).

The stack may reference other stacks of the stage (the network, the workspace); none of them may
reference it back. The stack and the logical IDs of its tables are recorded in
`tests/policy/stateful_logical_ids.json`: moving a table to another stack replaces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.pools.routing import Routing
from rhi.pools.tables import PoolTables

if TYPE_CHECKING:
    from constructs import Construct

    from rhi.config import Config
    from rhi.network.stack import NetworkStack


class PoolsStack(cdk.Stack):
    """Holds the pool-state and heartbeat tables, which the stack's later constructs take from
    `tables` (`tables.pool_state`, `tables.heartbeats`), and the internal load balancer with one
    target group per pool, which they take from `routing` (`routing.target_groups[name]`)."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config, network: NetworkStack) -> None:
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
