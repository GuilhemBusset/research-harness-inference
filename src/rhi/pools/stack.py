"""The pools stack: the pools' state and heartbeats, and later the internal load balancer, the
groups and the scaler that use them (system design, sections 2, 4.2 and 5).

The stack may reference other stacks of the stage (the network, the workspace); none of them may
reference it back. The stack and the logical IDs of its tables are recorded in
`tests/policy/stateful_logical_ids.json`: moving a table to another stack replaces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.pools.tables import PoolTables

if TYPE_CHECKING:
    from constructs import Construct


class PoolsStack(cdk.Stack):
    """Holds the pool-state and heartbeat tables, which the stack's later constructs take from
    `tables` (`tables.pool_state`, `tables.heartbeats`)."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.tables = PoolTables(self, "Tables")
