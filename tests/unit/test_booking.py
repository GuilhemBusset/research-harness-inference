"""What a check books a pool through (scripts/pipeline/booking.py): one instance, for the minutes
and under the name it is given, only on a pool that has no session booked or active; and a stop
that ends the session only while the item still reads as the booking that was made.

moto holds the pool-state table in process: no account, no credentials, no network.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

import booking
from rhi_common.records import SESSION_RECORD, Session
from rhi_common.sessions import SessionError, is_active
from rhi_common.tables import POOL_STATE

if TYPE_CHECKING:
    from collections.abc import Iterator

NOW = 1_800_000_000
HOURS = 12
NAME = "pipeline/verify"


@pytest.fixture
def table() -> Iterator[Any]:
    """The pool-state table, in moto, with the key schema of `rhi_common.tables.POOL_STATE` and no item."""
    with mock_aws():
        keys = [(POOL_STATE.partition_key, "HASH"), (POOL_STATE.sort_key, "RANGE")]
        yield boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="pool-state-under-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys if key is not None
            ],
            KeySchema=[{"AttributeName": key.name, "KeyType": kind} for key, kind in keys if key is not None],
        )


def stored(table: Any, pool: str = "mock") -> Session | None:
    item = table.get_item(Key={"pool": pool, "record": SESSION_RECORD}, ConsistentRead=True).get("Item")
    return None if item is None else Session.from_item(item)


def a_session(starts_at: int, ends_at: int, *, set_by: str = "admin", set_at: int = NOW - 7200) -> Session:
    return Session(pool="mock", instances=2, starts_at=starts_at, ends_at=ends_at, set_by=set_by, set_at=set_at)


def book(table: Any, *, minutes: int = 90, now: int = NOW) -> Session | None:
    return booking.book(table, "mock", minutes=minutes, set_by=NAME, now=now, session_max_hours=HOURS)


def test_a_pool_with_no_session_is_booked_for_one_instance_the_minutes_and_the_name_given(table: Any) -> None:
    booked = book(table, minutes=90)

    assert booked == Session(pool="mock", instances=1, starts_at=NOW, ends_at=NOW + 5400, set_by=NAME, set_at=NOW)
    assert stored(table) == booked and is_active(booked, NOW, session_max_hours=HOURS)
    # Another pool's item is not this pool's session.
    assert stored(table, "coursework") is None


def test_a_pool_whose_session_has_ended_is_booked_again(table: Any) -> None:
    table.put_item(Item=a_session(NOW - 3600, NOW).to_item())

    booked = book(table)

    assert booked is not None and stored(table) == booked and booked.set_by == NAME


@pytest.mark.parametrize(
    "held",
    [a_session(NOW + 600, NOW + 4200), a_session(NOW - 600, NOW + 1)],
    ids=["a session booked", "a session active"],
)
def test_a_pool_that_has_a_session_is_not_booked_and_its_session_is_left_as_it_is(table: Any, held: Session) -> None:
    table.put_item(Item=held.to_item())

    assert book(table) is None
    assert stored(table) == held


class BookedMeanwhile:
    """The table, on which `other` is written between the booking's read and its write."""

    def __init__(self, table: Any, other: Session) -> None:
        self.table, self.other = table, other

    def get_item(self, **request: Any) -> dict[str, Any]:
        answer = self.table.get_item(**request)
        self.table.put_item(Item=self.other.to_item())
        return answer

    def put_item(self, **request: Any) -> dict[str, Any]:
        return self.table.put_item(**request)


@pytest.mark.parametrize("ended", [None, a_session(NOW - 3600, NOW)], ids=["no item", "an ended session"])
def test_a_session_booked_between_the_read_and_the_write_is_not_written_over(table: Any, ended: Session | None) -> None:
    if ended is not None:
        table.put_item(Item=ended.to_item())
    other = a_session(NOW, NOW + 3600, set_at=NOW)

    assert book(BookedMeanwhile(table, other)) is None
    assert stored(table) == other


def test_a_booking_longer_than_a_session_may_last_is_refused_and_nothing_is_written(table: Any) -> None:
    with pytest.raises(SessionError):
        book(table, minutes=HOURS * 60 + 1)
    assert stored(table) is None
    assert book(table, minutes=HOURS * 60) is not None


def test_the_booking_is_stopped_at_the_time_of_the_stop(table: Any) -> None:
    booked = book(table)
    assert booked is not None

    ended = booking.stop(table, booked, now=NOW + 300, session_max_hours=HOURS)

    assert ended == dataclasses.replace(booked, ends_at=NOW + 300, set_at=NOW + 300)
    assert stored(table) == ended and not is_active(ended, NOW + 300, session_max_hours=HOURS)
    # Stopped once: a second stop finds an item that is no longer the booking.
    assert booking.stop(table, booked, now=NOW + 600, session_max_hours=HOURS) is None
    assert stored(table) == ended


@pytest.mark.parametrize(
    "change",
    [
        {"instances": 2},
        {"starts_at": NOW + 60},
        {"ends_at": NOW + 7200},
        {"set_by": "admin"},
        {"set_at": NOW + 1},
    ],
    ids=lambda change: next(iter(change)),
)
def test_a_session_that_no_longer_reads_as_the_booking_is_not_stopped(table: Any, change: dict[str, Any]) -> None:
    booked = book(table)
    assert booked is not None
    theirs = dataclasses.replace(booked, **change)
    table.put_item(Item=theirs.to_item())

    assert booking.stop(table, booked, now=NOW + 300, session_max_hours=HOURS) is None
    assert stored(table) == theirs
    # Nor when the change comes between the stop's read and its write: the write's own condition
    # compares every one of them.
    table.put_item(Item=booked.to_item())
    assert booking.stop(BookedMeanwhile(table, theirs), booked, now=NOW + 300, session_max_hours=HOURS) is None
    assert stored(table) == theirs


def test_a_booking_that_is_gone_or_has_ended_is_not_written(table: Any) -> None:
    booked = book(table, minutes=10)
    assert booked is not None
    # Its end has come: there is nothing to stop.
    assert booking.stop(table, booked, now=NOW + 600, session_max_hours=HOURS) is None
    assert stored(table) == booked
    table.delete_item(Key={"pool": "mock", "record": SESSION_RECORD})
    assert booking.stop(table, booked, now=NOW + 60, session_max_hours=HOURS) is None
    assert stored(table) is None


def test_the_table_is_found_by_its_logical_id_in_the_stack() -> None:
    class CloudFormation:
        def describe_stack_resource(self, **request: str) -> dict[str, Any]:
            assert request == {"StackName": "Rhi-Pools", "LogicalResourceId": "TablesPoolState68A872F7"}
            return {"StackResourceDetail": {"PhysicalResourceId": "Rhi-Pools-TablesPoolState68A872F7-EXAMPLE"}}

    found = booking.table_name(CloudFormation(), "Rhi-Pools", "TablesPoolState68A872F7")
    assert found == "Rhi-Pools-TablesPoolState68A872F7-EXAMPLE"
