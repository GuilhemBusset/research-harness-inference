"""Admission must remain safe while a new-day opener checks its snapshot."""

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
from test_issue85_open_day_race_probe import PauseOperation


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


class PausedPut:
    """Pause before publishing a day and after it is visible to other callers."""

    def __init__(self, raw: Client) -> None:
        self.raw = raw
        self.before = threading.Event()
        self.publish = threading.Event()
        self.after = threading.Event()
        self.finish = threading.Event()

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> Any:
            if name != "put_item":
                return getattr(self.raw, name)(**kwargs)
            self.before.set()
            assert self.publish.wait(timeout=10)
            result = self.raw.put_item(**kwargs)
            self.after.set()
            assert self.finish.wait(timeout=10)
            return result

        return call


def test_followers_cannot_use_slots_missing_from_an_unconfirmed_day(client: Client) -> None:
    clock = Clock(MIDNIGHT - 60)
    who = student(limit=4)
    ordinary = DynamoDBLedger(client, TABLE, clock=clock)
    original = [reserve(ordinary, who, f"live-{number}") for number in range(2)]
    assert all(isinstance(answer, Reservation) for answer in original)
    old_clients = [PauseOperation(client, "update_item") for _ in range(2)]
    opener_client = PausedPut(client)
    with ThreadPoolExecutor(max_workers=3) as threads:
        try:
            clock.now = MIDNIGHT - 1
            old = [
                threads.submit(reserve, DynamoDBLedger(raw, TABLE, clock=clock), who, f"late-old-{number}")
                for number, raw in enumerate(old_clients)
            ]
            for raw in old_clients:
                assert raw.reached.wait(timeout=10)
            clock.now = MIDNIGHT + 1
            opener = threads.submit(reserve, DynamoDBLedger(opener_client, TABLE, clock=clock), who, "opener")
            assert opener_client.before.wait(timeout=10)
            for raw in old_clients:
                raw.resume.set()
            old_answers = [answer.result(timeout=10) for answer in old]
            # Both old-day calls have returned before today's item exists.
            opener_client.publish.set()
            assert opener_client.after.wait(timeout=10)
            # The new item is visible, but its opener has not checked for late old-day writes.
            follower = reserve(ordinary, who, "follower")
            opener_client.finish.set()
            opener_answer = opener.result(timeout=10)
        finally:
            for raw in old_clients:
                raw.resume.set()
            opener_client.publish.set()
            opener_client.finish.set()

    answers = [*original, *old_answers, follower, opener_answer]
    assert sum(isinstance(answer, Reservation) for answer in answers) <= who.concurrency_limit, answers
    assert sum(answer is Cause.CONCURRENCY_LIMIT for answer in answers) >= 2
