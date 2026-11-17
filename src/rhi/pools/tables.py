"""The pool tables: pool-state and heartbeats (system design, sections 2 and 8.5).

Each table is built from its entry in `rhi_common.tables`, which the scaler, the gateway and the
admin CLI read too, with the billing and the protections of every table of the stage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from constructs import Construct

from rhi.access.tables import protected_table
from rhi_common.tables import HEARTBEATS, POOL_STATE

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb


class PoolTables(Construct):
    """The pool-state and heartbeat tables, which the stack's later constructs take from
    `pool_state` and `heartbeats`, or by the schema's name from `by_name`.

    The construct IDs are part of the tables' logical IDs, which `tests/policy/` records: renaming
    one replaces its table."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.pool_state = protected_table(self, "PoolState", POOL_STATE)
        self.heartbeats = protected_table(self, "Heartbeats", HEARTBEATS)
        self.by_name: dict[str, dynamodb.Table] = {
            POOL_STATE.name: self.pool_state,
            HEARTBEATS.name: self.heartbeats,
        }
