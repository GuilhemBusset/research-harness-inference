"""Independent schema, conversion and attribute-guard evidence for issue 81."""

from __future__ import annotations

import inspect
import runpy
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import cast

import pytest

from rhi_common import records, students, tables

type StudentRecord = students.StudentKey | students.RosterEntry | students.UsageDay


def student_items() -> list[tuple[type[StudentRecord], dict[str, object]]]:
    """Item forms specified independently of the serializers."""
    return [
        (students.StudentKey, {
            "key_hash": "cd" * 32, "github_id": 97, "alias": "ibis", "installation": "terminal-b",
            "issued_at": 100, "expires_at": 200, "revoked_at": 150,
            "daily_token_budget": 8000, "concurrency_limit": 3,
        }),
        (students.RosterEntry, {
            "github_id": 97, "alias": "ibis", "daily_token_budget": 8000,
            "concurrency_limit": 3, "added_at": 50, "expires_at": 300,
        }),
        (students.UsageDay, {
            "github_id": 97, "day": "2026-11-15", "alias": "ibis", "tokens": 23, "expires_at": 300,
            "leases": {
                "first": {"tokens": 11, "estimate": 7, "expires": 120},
                "second": {"tokens": 12, "estimate": 5, "expires": 130},
            },
            "affinity_cookie": {"mock": "cookie-value"}, "rebalance_applied": {"mock": 4},
        }),
    ]


def decimal_item(item: Mapping[str, object]) -> dict[str, object]:
    return {
        name: decimal_item(value) if isinstance(value, Mapping) else Decimal(value) if type(value) is int else value
        for name, value in item.items()
    }


def test_all_table_definitions_match_the_issue() -> None:
    schemas = {
        table.name: (
            (table.partition_key.name, table.partition_key.type.value),
            None if table.sort_key is None else (table.sort_key.name, table.sort_key.type.value),
            {
                index.name: (
                    (index.partition_key.name, index.partition_key.type.value),
                    None if index.sort_key is None else (index.sort_key.name, index.sort_key.type.value),
                )
                for index in table.indexes
            },
            table.expiry_attribute,
        )
        for table in tables.TABLES
    }
    assert schemas == {
        "keys": (("key_hash", "S"), None, {"by_student": (("github_id", "N"), ("issued_at", "N"))}, "expires_at"),
        "roster": (("github_id", "N"), None, {"by_alias": (("alias", "S"), None)}, "expires_at"),
        "usage": (("github_id", "N"), ("day", "S"), {}, "expires_at"),
        "pool-state": (("pool", "S"), ("record", "S"), {}, "expires_at"),
        "heartbeats": (("pool", "S"), ("source", "S"), {}, "expires_at"),
    }
    assert set(tables.POOL_STATE_RECORDS) == {"mode", "note", "memory"}
    assert tables.MEMORY_RECORD == "memory"
    assert tables.LAUNCH_SUSPENDED == "launch_suspended"


def test_student_forms_round_trip_without_extra_attributes() -> None:
    for cls, item in student_items():
        decoded = cls.from_item(item)
        assert decoded.to_item() == item
        assert cls.from_item(decoded.to_item()) == decoded
        assert cls.from_item(decimal_item(item)) == decoded
    usage = students.UsageDay.from_item(decimal_item(student_items()[2][1]))
    assert usage.leases == {
        "first": students.Lease(tokens=11, estimate=7, expires=120),
        "second": students.Lease(tokens=12, estimate=5, expires=130),
    }


@pytest.mark.parametrize(
    "required", ["key_hash", "github_id", "alias", "issued_at", "expires_at", "daily_token_budget", "concurrency_limit"]
)
def test_missing_key_fields_are_refused(required: str) -> None:
    item = student_items()[0][1]
    del item[required]
    with pytest.raises(records.RecordError):
        students.StudentKey.from_item(decimal_item(item))


def test_every_item_has_table_and_index_keys() -> None:
    samples = [
        *[(table, cls.from_item(item)) for table, (cls, item) in zip(
            (tables.KEYS, tables.ROSTER, tables.USAGE), student_items(), strict=True
        )],
        (tables.POOL_STATE, records.PoolState("mock", records.Mode.ENABLED, "admin", 100)),
        (tables.POOL_STATE, records.ScalerNote("mock", 1, None, 100)),
        (tables.HEARTBEATS, records.GatewayHeartbeat("task-b", "mock", 1, 0, None, 100)),
        (tables.HEARTBEATS, records.InstanceHeartbeat("node-b", "mock", 1, 0, "digest", "revision", "serving", 100)),
    ]
    classes = {
        cls for module in (records, students)
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if cls.__module__ == module.__name__ and hasattr(cls, "to_item")
    }
    assert {type(sample) for _, sample in samples} == classes
    for table, sample in samples:
        item = sample.to_item()
        keys = [table.partition_key, table.sort_key]
        for index in table.indexes:
            keys.extend([index.partition_key, index.sort_key])
        for key in keys:
            if key is not None:
                assert type(item[key.name]) is {"S": str, "N": int}[key.type.value]


@pytest.mark.parametrize(
    ("item_type", "method"),
    [(students.StudentKey, "to_item"), (students.RosterEntry, "to_item"),
     (students.UsageDay, "to_item"), (students.Lease, "to_map")],
)
def test_named_attribute_guard_rejects_an_extra_written_attribute(
    monkeypatch: pytest.MonkeyPatch, item_type: type, method: str
) -> None:
    namespace = runpy.run_path("tests/unit/common/test_students.py")
    guard = namespace["test_attributes_are_the_listed_ones"]
    guard()
    original = cast(Callable[[object], Mapping[str, object]], getattr(item_type, method))

    def write_extra(record: object) -> dict[str, object]:
        return {**original(record), "unlisted_probe_attribute": "probe"}

    with monkeypatch.context() as mutation:
        mutation.setattr(item_type, method, write_extra)
        with pytest.raises(AssertionError):
            guard()
    guard()
