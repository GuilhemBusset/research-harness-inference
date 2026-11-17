"""The student tables: keys, roster and usage (system design, sections 2 and 8.5).

Each table is built from its entry in `rhi_common.tables`, which the issuer, the admin CLI and the
gateway read too, so that the table and its writers cannot disagree on a key. `protected_table`
gives every table of the stage the same billing and the same protections: there is one
environment, so a lost table is lost state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

from rhi_common.tables import KEYS, ROSTER, USAGE, AttributeType, KeyAttribute

if TYPE_CHECKING:
    from rhi_common.tables import Table

_TYPES = {AttributeType.STRING: dynamodb.AttributeType.STRING, AttributeType.NUMBER: dynamodb.AttributeType.NUMBER}


def _attribute(key: KeyAttribute) -> dynamodb.Attribute:
    return dynamodb.Attribute(name=key.name, type=_TYPES[key.type])


def _sort_attribute(key: KeyAttribute | None) -> dynamodb.Attribute | None:
    return None if key is None else _attribute(key)


def protected_table(scope: Construct, construct_id: str, schema: Table) -> dynamodb.Table:
    """A table with the keys, the indexes and the expiry attribute of `schema`, billed on demand,
    with point-in-time recovery and deletion protection, and retained on deletion and on
    replacement. The deployment names it: `schema.name` is the name the code knows it by."""
    table = dynamodb.Table(
        scope,
        construct_id,
        partition_key=_attribute(schema.partition_key),
        sort_key=_sort_attribute(schema.sort_key),
        time_to_live_attribute=schema.expiry_attribute,
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True
        ),
        deletion_protection=True,
        removal_policy=cdk.RemovalPolicy.RETAIN,
    )
    for index in schema.indexes:
        table.add_global_secondary_index(
            index_name=index.name,
            partition_key=_attribute(index.partition_key),
            sort_key=_sort_attribute(index.sort_key),
        )
    return table


class AccessTables(Construct):
    """The keys, roster and usage tables, which the stack's later constructs take from `keys`,
    `roster` and `usage`, or by the schema's name from `by_name`.

    The construct IDs are part of the tables' logical IDs, which `tests/policy/` records: renaming
    one replaces its table."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.keys = protected_table(self, "Keys", KEYS)
        self.roster = protected_table(self, "Roster", ROSTER)
        self.usage = protected_table(self, "Usage", USAGE)
        self.by_name: dict[str, dynamodb.Table] = {
            KEYS.name: self.keys,
            ROSTER.name: self.roster,
            USAGE.name: self.usage,
        }
