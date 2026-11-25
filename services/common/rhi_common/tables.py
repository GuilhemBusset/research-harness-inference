"""The five tables and their keys (system design, sections 2 and 8.5).

The stack creates the tables, and the issuer, the admin CLI, the gateway, the scaler and the
lifecycle agent read and write them. A key cannot change once its table exists without replacing
the table, so every key is named here and nowhere else. Nothing here calls AWS or imports the CDK:
a table is plain data that the stack turns into a construct and that a reader turns into a query.

| Table | Partition key | Sort key | Secondary index | What an item is |
| --- | --- | --- | --- | --- |
| keys | `key_hash` (string) | none | `by_student`: `github_id` (number), sorted by `issued_at` (number) | One API key |
| roster | `github_id` (number) | none | `by_alias`: `alias` (string) | One student |
| usage | `github_id` (number) | `day` (string) | none | One student's day |
| pool-state | `pool` (string) | `record` (string) | none | A pool's `mode`, `note`, `memory` and `session`; the session switch |
| heartbeats | `pool` (string) | `source` (string) | none | One gateway task's or one instance's heartbeat |

A table's `name` is the name the code knows it by, not the name of the deployed table, which the
deployment gives. Every table's expiry attribute is `expires_at`, whole seconds since the epoch;
an item without it does not expire. The items of keys, roster and usage are in
`rhi_common.students`, those of heartbeats and the `mode`, `note` and `session` of pool-state in
`rhi_common.records`. The record `memory` of pool-state holds the scaler's memory of earlier runs:
its attributes are the scaler's own, except `launch_suspended`, the mark the admin CLI clears.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from rhi_common.records import MODE_RECORD, NOTE_RECORD, SESSION_PARTITION, SESSION_RECORD, SWITCH_RECORD

# The expiry attribute of every table.
EXPIRES_AT = "expires_at"

# The sort keys of the pool-state table: the admin's record, the scaler's note to the gateway, the
# scaler's memory of earlier runs, and the pool's session (`rhi_common.records.Session`).
MEMORY_RECORD = "memory"
POOL_STATE_RECORDS = (MODE_RECORD, NOTE_RECORD, MEMORY_RECORD, SESSION_RECORD)
# The item of the session switch (`rhi_common.records.SessionSwitch`), beside the pools' records:
# its partition key, which no pool's name can be, and its sort key.
SESSION_SWITCH_KEY = {"pool": SESSION_PARTITION, "record": SWITCH_RECORD}
# The one attribute of the record `memory` that another component than the scaler writes.
LAUNCH_SUSPENDED = "launch_suspended"


class AttributeType(StrEnum):
    """The type of a key attribute, under DynamoDB's own letter for it."""

    STRING = "S"
    NUMBER = "N"


@dataclass(frozen=True)
class KeyAttribute:
    """An attribute that is part of a key, and its type."""

    name: str
    type: AttributeType


@dataclass(frozen=True)
class Index:
    """A global secondary index."""

    name: str
    partition_key: KeyAttribute
    sort_key: KeyAttribute | None = None


@dataclass(frozen=True)
class Table:
    """A table: its keys, its indexes and its expiry attribute."""

    name: str
    partition_key: KeyAttribute
    sort_key: KeyAttribute | None = None
    indexes: tuple[Index, ...] = ()
    expiry_attribute: str = EXPIRES_AT

    def index(self, name: str) -> Index:
        """The index of that name; a `KeyError` if the table has none."""
        for index in self.indexes:
            if index.name == name:
                return index
        raise KeyError(name)


def _string(name: str) -> KeyAttribute:
    return KeyAttribute(name, AttributeType.STRING)


def _number(name: str) -> KeyAttribute:
    return KeyAttribute(name, AttributeType.NUMBER)


BY_STUDENT = "by_student"
BY_ALIAS = "by_alias"

KEYS = Table(
    name="keys",
    partition_key=_string("key_hash"),
    indexes=(Index(BY_STUDENT, partition_key=_number("github_id"), sort_key=_number("issued_at")),),
)
ROSTER = Table(
    name="roster",
    partition_key=_number("github_id"),
    indexes=(Index(BY_ALIAS, partition_key=_string("alias")),),
)
USAGE = Table(name="usage", partition_key=_number("github_id"), sort_key=_string("day"))
POOL_STATE = Table(name="pool-state", partition_key=_string("pool"), sort_key=_string("record"))
HEARTBEATS = Table(name="heartbeats", partition_key=_string("pool"), sort_key=_string("source"))

TABLES = (KEYS, ROSTER, USAGE, POOL_STATE, HEARTBEATS)
