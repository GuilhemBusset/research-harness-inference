"""The access stack: the students' state, and later the issuer and the admin's sign-in (system
design, sections 2 and 6.4).

The stack and the logical IDs of its tables are recorded in `tests/policy/stateful_logical_ids.json`:
moving a table to another stack replaces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.access.tables import AccessTables

if TYPE_CHECKING:
    from constructs import Construct


class AccessStack(cdk.Stack):
    """Holds the keys, roster and usage tables, which the other stacks of the stage take from
    `tables` (`tables.keys`, ...)."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.tables = AccessTables(self, "Tables")
