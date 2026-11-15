"""Independent request-budget contract checks for issue 85."""

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
from rhi_gateway.budget import DynamoDBLedger, InMemoryLedger, Ledger, Reservation, estimate_input_tokens
from rhi_gateway.errors import GatewayError

NOW = int(datetime(2026, 11, 15, 12, tzinfo=UTC).timestamp())
BUDGET = 100_000_000
BODY = b'{"messages":[{"role":"user","content":"hello"}]}'
ESTIMATE = estimate_input_tokens(BODY)
OUTPUT = 500
AMOUNT = ESTIMATE + OUTPUT


@dataclass
class Clock:
    now: int = NOW

    def __call__(self) -> float:
        return self.now


class AtomicClient:
    """Give each moto operation the item atomicity DynamoDB guarantees."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.lock = threading.Lock()
        self.writes = 0

    def __getattr__(self, name: str) -> Any:
        operation = getattr(self.client, name)

        def call(**kwargs: Any) -> Any:
            with self.lock:
                result = operation(**kwargs)
                if name in {"put_item", "update_item"}:
                    self.writes += 1
                return result

        return call


@dataclass
class Harness:
    ledger: Ledger
    clock: Clock
    storage: InMemoryLedger | AtomicClient

    @property
    def writes(self) -> int:
        return self.storage.writes


@pytest.fixture(params=["memory", "dynamodb"])
def harness(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    clock = Clock()
    if request.param == "memory":
        memory = InMemoryLedger(clock=clock)
        yield Harness(memory, clock, memory)
        return
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        client = boto3.client("dynamodb")
        assert USAGE.sort_key is not None
        keys = [(USAGE.partition_key, "HASH"), (USAGE.sort_key, "RANGE")]
        client.create_table(
            TableName="budget-probe",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys],
            KeySchema=[{"AttributeName": key.name, "KeyType": role} for key, role in keys],
        )
        atomic = AtomicClient(client)
        yield Harness(DynamoDBLedger(atomic, "budget-probe", clock=clock), clock, atomic)


def student(*, budget: int = BUDGET, limit: int = 4) -> StudentKey:
    return StudentKey(
        key_hash=hashlib.sha256(b"budget probe").hexdigest(),
        github_id=19,
        alias="probe-student",
        installation=None,
        issued_at=NOW - 86_400,
        expires_at=NOW + 30 * 86_400,
        revoked_at=None,
        daily_token_budget=budget,
        concurrency_limit=limit,
    )


def reserve(ledger: Ledger, who: StudentKey, request_id: str) -> Reservation:
    return ledger.reserve(
        who, request_id, BODY, output_limit=OUTPUT, hold_seconds=420, longest_request_seconds=1800
    )


def test_atomic_admission_and_budget_refusal(harness: Harness) -> None:
    ledger = harness.ledger
    who = student(budget=AMOUNT * 10, limit=50)
    start = threading.Barrier(50)

    def one(number: int) -> Reservation | Cause:
        start.wait()
        try:
            return reserve(ledger, who, f"parallel-{number}")
        except GatewayError as error:
            return error.cause

    with ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(one, range(50)))
    assert sum(isinstance(result, Reservation) for result in results) == 10
    assert results.count(Cause.BUDGET_REACHED) == 40
    assert ledger.tokens_left(who) == 0
    assert harness.writes == 10


def test_four_slots_and_settlement_releases_one(harness: Harness) -> None:
    ledger = harness.ledger
    who = student()
    held = [reserve(ledger, who, f"slot-{number}") for number in range(4)]
    with pytest.raises(GatewayError) as error:
        reserve(ledger, who, "fifth")
    assert error.value.cause is Cause.CONCURRENCY_LIMIT
    ledger.settle(held[0], input_tokens=3, output_tokens=7, ended_cleanly=True)
    reserve(ledger, who, "after-settlement")


@pytest.mark.parametrize("clean, observed, charged", [(True, 3, 3), (False, 1, ESTIMATE), (False, 1000, 1000)])
def test_settlement_charge_and_idempotence(harness: Harness, clean: bool, observed: int, charged: int) -> None:
    ledger = harness.ledger
    who = student()
    held = reserve(ledger, who, "settlement")
    assert held.lease.estimate == ESTIMATE
    assert held.lease.tokens == AMOUNT
    assert held.day == "2026-11-15"
    assert ledger.tokens_left(who) == BUDGET - AMOUNT
    assert ledger.settle(held, input_tokens=0, output_tokens=observed, ended_cleanly=clean) == charged
    assert ledger.tokens_left(who) == BUDGET - charged
    assert ledger.settle(held, input_tokens=8000, output_tokens=700, ended_cleanly=True) is None
    assert ledger.tokens_left(who) == BUDGET - charged
    assert harness.writes == 2


def test_disconnect_after_100_observed_tokens(harness: Harness) -> None:
    ledger = harness.ledger
    who = student()
    held = ledger.reserve(
        who, "disconnect", b"x" * 8000, output_limit=500, hold_seconds=420, longest_request_seconds=1800
    )
    charge = max(100, held.lease.estimate)
    assert ledger.settle(held, input_tokens=0, output_tokens=100, ended_cleanly=False) == charge
    assert ledger.settle(held, input_tokens=9000, output_tokens=500, ended_cleanly=True) is None
    assert ledger.tokens_left(who) == BUDGET - charge


def test_whole_cached_input_is_charged(harness: Harness) -> None:
    ledger = harness.ledger
    who = student()
    before = ledger.tokens_left(who)
    held = reserve(ledger, who, "cached")
    assert ledger.settle(held, input_tokens=10_000 + 80_000, output_tokens=500, ended_cleanly=True) == 90_500
    assert before - ledger.tokens_left(who) == 90_500


def test_two_writes_and_read_only_balance(harness: Harness) -> None:
    ledger = harness.ledger
    who = student()
    assert ledger.tokens_left(who) == BUDGET
    assert harness.writes == 0
    held = reserve(ledger, who, "two-writes")
    assert harness.writes == 1
    ledger.settle(held, input_tokens=4, output_tokens=8, ended_cleanly=True)
    assert harness.writes == 2
    for _ in range(3):
        assert ledger.tokens_left(who) == BUDGET - 12
    assert harness.writes == 2


def test_expiry_frees_slot_and_charges_estimate(harness: Harness) -> None:
    ledger = harness.ledger
    who = student(budget=2 * AMOUNT, limit=1)
    first = reserve(ledger, who, "expires")
    assert first.lease.expires == NOW + 2280
    harness.clock.now = NOW + 2279
    with pytest.raises(GatewayError) as error:
        reserve(ledger, who, "too-soon")
    assert error.value.cause is Cause.CONCURRENCY_LIMIT
    harness.clock.now = NOW + 2281
    assert ledger.tokens_left(who) == who.daily_token_budget - ESTIMATE
    assert harness.writes == 1
    second = reserve(ledger, who, "after-expiry")
    assert ledger.tokens_left(who) == who.daily_token_budget - ESTIMATE - AMOUNT
    assert ledger.settle(first, input_tokens=9000, output_tokens=0, ended_cleanly=True) is None
    ledger.settle(second, input_tokens=2, output_tokens=3, ended_cleanly=True)
    assert ledger.tokens_left(who) == who.daily_token_budget - ESTIMATE - 5



def test_caller_durations_determine_expiry(harness: Harness) -> None:
    who = student()
    held = harness.ledger.reserve(
        who, "custom-duration", BODY, output_limit=OUTPUT, hold_seconds=17, longest_request_seconds=29
    )
    assert held.lease.expires == harness.clock.now + 17 + 29 + 60
    harness.clock.now = held.lease.expires
    assert harness.ledger.settle(held, input_tokens=9000, output_tokens=100, ended_cleanly=True) == ESTIMATE
    assert harness.ledger.tokens_left(who) == BUDGET - ESTIMATE


def test_daily_budget_resets_on_utc_day(harness: Harness) -> None:
    who = student()
    harness.clock.now = NOW + 12 * 3600 - 30
    held = reserve(harness.ledger, who, "prior-day")
    harness.clock.now += 60
    assert harness.ledger.tokens_left(who) == BUDGET
    assert harness.ledger.settle(held, input_tokens=17, output_tokens=5, ended_cleanly=True) == 22
    assert harness.ledger.tokens_left(who) == BUDGET

def test_midnight_does_not_free_live_concurrency_slots(harness: Harness) -> None:
    ledger = harness.ledger
    who = student(limit=4)
    harness.clock.now = NOW + 12 * 3600 - 30
    for number in range(4):
        held = reserve(ledger, who, f"before-midnight-{number}")
        assert held.lease.expires > harness.clock.now + 60
    harness.clock.now += 60
    with pytest.raises(GatewayError) as error:
        reserve(ledger, who, "after-midnight")
    assert error.value.cause is Cause.CONCURRENCY_LIMIT
