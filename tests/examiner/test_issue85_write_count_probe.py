"""Count actual record mutations when a reservation crosses UTC midnight."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.students import StudentKey
from rhi_common.tables import USAGE
from rhi_gateway.budget import DynamoDBLedger, InMemoryLedger, Ledger

MIDNIGHT = int(datetime(2026, 11, 16, tzinfo=UTC).timestamp())


@dataclass
class Clock:
    """A write begins before midnight and its answer arrives after midnight."""

    reads: int = 0

    def __call__(self) -> float:
        self.reads += 1
        return MIDNIGHT - 1 if self.reads == 1 else MIDNIGHT + 1


class Writes:
    """Count successful SDK mutations; failed conditions do not change the record."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.writes = 0

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> Any:
            result = getattr(self.client, name)(**kwargs)
            if name in {"put_item", "update_item"}:
                self.writes += 1
            return result

        return call


@pytest.fixture(params=["memory", "dynamodb"])
def ledger_and_counter(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Ledger, InMemoryLedger | Writes]]:
    clock = Clock()
    if request.param == "memory":
        memory = InMemoryLedger(clock=clock)
        yield memory, memory
        return
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        raw = boto3.client("dynamodb")
        assert USAGE.sort_key is not None
        keys = [(USAGE.partition_key, "HASH"), (USAGE.sort_key, "RANGE")]
        raw.create_table(
            TableName="midnight-write-count",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys],
            KeySchema=[{"AttributeName": key.name, "KeyType": role} for key, role in keys],
        )
        counter = Writes(raw)
        yield DynamoDBLedger(counter, "midnight-write-count", clock=clock), counter


def test_midnight_request_still_makes_exactly_two_writes(
    ledger_and_counter: tuple[Ledger, InMemoryLedger | Writes],
) -> None:
    ledger, counter = ledger_and_counter
    student = StudentKey(
        key_hash=hashlib.sha256(b"write-count probe").hexdigest(),
        github_id=31,
        alias="probe-student",
        installation=None,
        issued_at=MIDNIGHT - 86400,
        expires_at=MIDNIGHT + 30 * 86400,
        revoked_at=None,
        daily_token_budget=100_000_000,
        concurrency_limit=4,
    )
    held = ledger.reserve(
        student, "crossing", b"x" * 4000, output_limit=500, hold_seconds=420, longest_request_seconds=1800
    )
    ledger.settle(held, input_tokens=1000, output_tokens=100, ended_cleanly=True)
    writes_after_settlement = counter.writes
    ledger.tokens_left(student)
    assert counter.writes == writes_after_settlement
    assert writes_after_settlement == 2
