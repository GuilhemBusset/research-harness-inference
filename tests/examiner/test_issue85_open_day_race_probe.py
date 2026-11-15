"""Exercise a new-day opener paused after reading yesterday's leases."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.causes import Cause
from rhi_common.tables import USAGE
from rhi_gateway.budget import DynamoDBLedger, Reservation
from test_issue85_followup_probe import MIDNIGHT, TABLE, Client, Clock, reserve, student


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Client]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        raw = boto3.client("dynamodb")
        assert USAGE.sort_key is not None
        keys = [(USAGE.partition_key, "HASH"), (USAGE.sort_key, "RANGE")]
        raw.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys],
            KeySchema=[{"AttributeName": key.name, "KeyType": role} for key, role in keys],
        )
        yield Client(raw)


class PauseOperation:
    """Delay one operation before it reaches the atomic moto client."""

    def __init__(self, raw: Client, operation: str) -> None:
        self.raw = raw
        self.operation = operation
        self.reached = threading.Event()
        self.resume = threading.Event()

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> Any:
            if name == self.operation and not self.reached.is_set():
                self.reached.set()
                assert self.resume.wait(timeout=10)
            return getattr(self.raw, name)(**kwargs)

        return call


def test_a_pending_day_opener_cannot_miss_a_confirmed_old_day_reservation(client: Client) -> None:
    clock = Clock(MIDNIGHT - 60)
    who = student(limit=4)
    ordinary = DynamoDBLedger(client, TABLE, clock=clock)
    for number in range(3):
        assert isinstance(reserve(ordinary, who, f"live-{number}"), Reservation)

    old_client = PauseOperation(client, "update_item")
    new_client = PauseOperation(client, "put_item")
    with ThreadPoolExecutor(max_workers=2) as threads:
        try:
            clock.now = MIDNIGHT - 1
            old = threads.submit(reserve, DynamoDBLedger(old_client, TABLE, clock=clock), who, "late-old")
            assert old_client.reached.wait(timeout=10)

            clock.now = MIDNIGHT + 1
            new = threads.submit(reserve, DynamoDBLedger(new_client, TABLE, clock=clock), who, "new-day")
            # The opener has read the three old leases but has not written today's item.
            assert new_client.reached.wait(timeout=10)
            old_client.resume.set()
            old_answer = old.result(timeout=10)
            # The old reservation has completed its post-write checks before today's item exists.
            new_client.resume.set()
            new_answer = new.result(timeout=10)
        finally:
            old_client.resume.set()
            new_client.resume.set()

    answers = [old_answer, new_answer]
    assert sum(isinstance(answer, Reservation) for answer in answers) == 1
    assert answers.count(Cause.CONCURRENCY_LIMIT) == 1
