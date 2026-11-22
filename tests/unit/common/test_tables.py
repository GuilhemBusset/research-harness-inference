"""The five tables and their keys (`rhi_common.tables`), and every item against its table's keys."""

from __future__ import annotations

import inspect
from collections.abc import Mapping

import pytest

from rhi_common import records, students, tables
from rhi_common.records import GatewayHeartbeat, InstanceHeartbeat, Mode, PoolState, ScalerNote, SessionSwitch
from rhi_common.students import Lease, RosterEntry, StudentKey, UsageDay
from rhi_common.tables import HEARTBEATS, KEYS, POOL_STATE, ROSTER, USAGE, AttributeType, KeyAttribute, Table

NOW = 1_800_000_000

type Record = (
    StudentKey | RosterEntry | UsageDay | PoolState | ScalerNote | SessionSwitch | GatewayHeartbeat | InstanceHeartbeat
)

# One of every item of `rhi_common.students` and `rhi_common.records`, with the table it goes to.
ITEMS: list[tuple[Table, Record]] = [
    (
        KEYS,
        StudentKey(
            key_hash="ab" * 32,
            github_id=4242,
            alias="otter",
            installation=None,
            issued_at=NOW,
            expires_at=NOW + 86400,
            revoked_at=None,
            daily_token_budget=2_000_000,
            concurrency_limit=4,
        ),
    ),
    (
        ROSTER,
        RosterEntry(
            github_id=4242,
            alias="otter",
            daily_token_budget=2_000_000,
            concurrency_limit=4,
            added_at=NOW,
            expires_at=NOW + 86400,
        ),
    ),
    (
        USAGE,
        UsageDay(
            github_id=4242,
            day="2027-01-15",
            alias="otter",
            tokens=10,
            expires_at=NOW + 86400,
            leases={"req-1": Lease(tokens=10, estimate=4, expires=NOW + 1800)},
        ),
    ),
    (POOL_STATE, PoolState(pool="mock", mode=Mode.ENABLED, set_by="admin", set_at=NOW)),
    (POOL_STATE, ScalerNote(pool="mock", capacity_asked=1, no_capacity_at=None, written_at=NOW)),
    (POOL_STATE, SessionSwitch(on=True, set_by="instructor", set_at=NOW)),
    (
        HEARTBEATS,
        GatewayHeartbeat(task="0f3a9c", pool="mock", in_flight=1, held=0, last_request_at=None, written_at=NOW),
    ),
    (
        HEARTBEATS,
        InstanceHeartbeat(
            instance="instance-1",
            pool="mock",
            running=1,
            waiting=0,
            image_digest="sha256:" + "ab" * 32,
            model_revision="rev-1",
            stage="serving",
            written_at=NOW,
        ),
    ),
]

PYTHON_TYPE = {AttributeType.STRING: str, AttributeType.NUMBER: int}


def carries(item: Mapping[str, object], key: KeyAttribute | None) -> bool:
    """Whether the item holds the key attribute with a value of its type; true of a key the table lacks."""
    return key is None or type(item.get(key.name)) is PYTHON_TYPE[key.type]


def test_the_tables_are_the_five_of_the_design() -> None:
    """The table of the issue's Context, written out: a key that changes here replaces a table."""
    described = {
        table.name: (
            (table.partition_key.name, table.partition_key.type.value),
            table.sort_key and (table.sort_key.name, table.sort_key.type.value),
            {
                index.name: (
                    (index.partition_key.name, index.partition_key.type.value),
                    index.sort_key and (index.sort_key.name, index.sort_key.type.value),
                )
                for index in table.indexes
            },
            table.expiry_attribute,
        )
        for table in tables.TABLES
    }
    assert described == {
        "keys": (
            ("key_hash", "S"),
            None,
            {"by_student": (("github_id", "N"), ("issued_at", "N"))},
            "expires_at",
        ),
        "roster": (("github_id", "N"), None, {"by_alias": (("alias", "S"), None)}, "expires_at"),
        "usage": (("github_id", "N"), ("day", "S"), {}, "expires_at"),
        "pool-state": (("pool", "S"), ("record", "S"), {}, "expires_at"),
        "heartbeats": (("pool", "S"), ("source", "S"), {}, "expires_at"),
    }
    assert len(tables.TABLES) == 5


def test_the_pool_state_records_and_the_mark_of_the_memory() -> None:
    assert set(tables.POOL_STATE_RECORDS) == {"mode", "note", "memory"}
    assert tables.MEMORY_RECORD == "memory"
    assert tables.LAUNCH_SUSPENDED == "launch_suspended"


def test_every_item_class_of_both_modules_is_checked() -> None:
    item_classes = {
        cls
        for module in (students, records)
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if cls.__module__ == module.__name__ and hasattr(cls, "to_item")
    }
    assert item_classes == {type(record) for _, record in ITEMS}


@pytest.mark.parametrize(("table", "record"), ITEMS, ids=lambda value: getattr(value, "name", type(value).__name__))
def test_every_item_carries_its_tables_keys(table: Table, record: Record) -> None:
    item = record.to_item()
    assert carries(item, table.partition_key)
    assert table.sort_key is None or table.sort_key.name in item
    assert carries(item, table.sort_key)
    # An item that expires does so by its table's expiry attribute, a whole number of seconds.
    if table.expiry_attribute in item:
        assert type(item[table.expiry_attribute]) is int


def test_the_pool_state_items_are_records_of_the_table() -> None:
    for table, record in ITEMS:
        if table is POOL_STATE:
            item = record.to_item()
            # A pool's record, or the session switch under the partition no pool can take.
            assert item["record"] in tables.POOL_STATE_RECORDS or (item["pool"], item["record"]) == tables.SESSION_SWITCH


def test_the_key_item_carries_the_keys_of_by_student() -> None:
    index = KEYS.index("by_student")
    item = ITEMS[0][1].to_item()
    assert index.sort_key is not None
    assert carries(item, index.partition_key)
    assert carries(item, index.sort_key)


def test_the_roster_item_carries_the_key_of_by_alias() -> None:
    index = ROSTER.index("by_alias")
    item = ITEMS[1][1].to_item()
    assert index.sort_key is None
    assert carries(item, index.partition_key)
