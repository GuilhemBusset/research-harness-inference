"""Independent checks of budget accounting and admission across midnight."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.causes import Cause
from rhi_common.students import StudentKey
from rhi_common.tables import USAGE
from rhi_gateway.budget import DynamoDBLedger, Ledger, Reservation
from rhi_gateway.errors import GatewayError

NOON = int(datetime(2026, 11, 15, 12, tzinfo=UTC).timestamp())
MIDNIGHT = NOON + 12 * 3600
BODY = b"x" * 8000
AMOUNT = 2500
TABLE = "budget-follow-up-probe"


@dataclass
class Clock:
    now: int = NOON

    def __call__(self) -> float:
        return self.now


class Client:
    """Serialize individual moto operations, and count successful writes only."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.lock = threading.Lock()
        self.writes = 0

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> Any:
            with self.lock:
                result = getattr(self.raw, name)(**kwargs)
                if name in {"put_item", "update_item"}:
                    self.writes += 1
                return result

        return call


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


def student(*, limit: int = 4) -> StudentKey:
    return StudentKey(
        key_hash=hashlib.sha256(b"follow-up probe").hexdigest(),
        github_id=23,
        alias="probe-student",
        installation=None,
        issued_at=NOON - 86_400,
        expires_at=NOON + 30 * 86_400,
        revoked_at=None,
        daily_token_budget=100 * AMOUNT,
        concurrency_limit=limit,
    )


def reserve(ledger: Ledger, who: StudentKey, request_id: str) -> Reservation | Cause:
    try:
        return ledger.reserve(
            who, request_id, BODY, output_limit=500, hold_seconds=420, longest_request_seconds=1800
        )
    except GatewayError as error:
        return error.cause


class Paused:
    """A task's client whose first write waits, once the SDK has been given it, until the test resumes it."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.reached = threading.Event()
        self.resume = threading.Event()

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> Any:
            if name in {"put_item", "update_item"} and not self.reached.is_set():
                self.reached.set()
                assert self.resume.wait(timeout=30)
            return getattr(self.client, name)(**kwargs)

        return call


def test_inflight_old_day_write_cannot_overbook_after_midnight(client: Client) -> None:
    clock = Clock(MIDNIGHT - 60)
    ledger = DynamoDBLedger(client, TABLE, clock=clock)
    who = student(limit=4)
    live = [reserve(ledger, who, f"live-{number}") for number in range(3)]
    assert all(isinstance(held, Reservation) for held in live)

    # A fourth reservation's write is with the SDK at 23:59:59, and stays there.
    clock.now = MIDNIGHT - 1
    paused = Paused(client)
    with ThreadPoolExecutor(max_workers=1) as executor:
        inflight = executor.submit(reserve, DynamoDBLedger(paused, TABLE, clock=clock), who, "inflight")
        assert paused.reached.wait(timeout=30)

        # At 00:00:01 another task opens the new day. Then the paused write goes on.
        clock.now = MIDNIGHT + 1
        after = reserve(ledger, who, "after-midnight")
        paused.resume.set()
        answers = [inflight.result(timeout=30), after]

    # The three requests are still live: one of the two is admitted, and the other is at the limit.
    assert sum(isinstance(answer, Reservation) for answer in answers) == 1
    assert answers.count(Cause.CONCURRENCY_LIMIT) == 1
    assert reserve(ledger, who, "a-sixth") is Cause.CONCURRENCY_LIMIT
