"""The DynamoDB ledger of `rhi_gateway.budget`, against moto (section 6.2, step 3).

moto evaluates the condition of a write in process, so these tests show the expressions the
ledger sends: the reservation's, the settlement's, and the one that removes expired leases. The
table is created with the key schema of `rhi_common.tables.USAGE`. Nothing here reaches AWS: the
credentials and the region are placeholders that moto never checks.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from rhi_common.causes import Cause
from rhi_common.pool_config import DEFAULT_HOLD_SECONDS
from rhi_common.students import Lease, StudentKey, UsageDay
from rhi_common.tables import USAGE
from rhi_gateway.budget import BYTES_PER_TOKEN, USAGE_RETENTION_SECONDS, DynamoDBLedger, Reservation
from rhi_gateway.errors import GatewayError, cause_of

DAILY_BUDGET = 100_000_000
LONGEST_REQUEST_SECONDS = 1800
NOON = int(datetime(2026, 11, 15, 12, tzinfo=UTC).timestamp())
COURSE_END = NOON + 30 * 86_400
TABLE = "usage-of-the-test"
KEY = {"github_id": {"N": "7"}, "day": {"S": "2026-11-15"}}


class Clock:
    def __init__(self, now: int = NOON) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Calls:
    """A DynamoDB client that names each call it makes, and makes one call at a time.

    DynamoDB checks a write's condition and applies the write as one step. moto does both in the
    thread of the caller with no lock, so two calls made at once can each pass a condition that
    only one of them should (and its tables are plain dictionaries, which fail when two threads
    change them at once). One call at a time gives moto what DynamoDB guarantees for one item, and
    no more: a reservation that takes several calls is still interleaved with the others.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._one_at_a_time = threading.Lock()
        self.made: list[str] = []

    def __getattr__(self, operation: str) -> Any:
        call = getattr(self._client, operation)

        def named(**arguments: Any) -> Any:
            with self._one_at_a_time:
                self.made.append(operation)
                return call(**arguments)

        return named


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Calls]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        dynamodb = boto3.client("dynamodb")
        keys = [(USAGE.partition_key, "HASH"), (USAGE.sort_key, "RANGE")]
        dynamodb.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys if key
            ],
            KeySchema=[{"AttributeName": key.name, "KeyType": role} for key, role in keys if key],
        )
        yield Calls(dynamodb)


def student(*, budget: int = DAILY_BUDGET, limit: int = 4) -> StudentKey:
    return StudentKey(
        key_hash=hashlib.sha256(b"not a key").hexdigest(),
        github_id=7,
        alias="student-07",
        installation=None,
        issued_at=NOON - 86_400,
        expires_at=COURSE_END,
        revoked_at=None,
        daily_token_budget=budget,
        concurrency_limit=limit,
    )


def reserve(
    ledger: DynamoDBLedger, who: StudentKey, request: str, *, estimate: int = 1000, output: int = 500
) -> Reservation:
    return ledger.reserve(
        who,
        request,
        b"x" * (estimate * BYTES_PER_TOKEN),
        output_limit=output,
        hold_seconds=DEFAULT_HOLD_SECONDS,
        longest_request_seconds=LONGEST_REQUEST_SECONDS,
    )


def refusal(ledger: DynamoDBLedger, who: StudentKey, request: str) -> Cause:
    with pytest.raises(GatewayError) as refused:
        reserve(ledger, who, request)
    return refused.value.cause


def written(day: str = "2026-11-15") -> UsageDay:
    """The student's item of a day, read with the table resource, as the other components read it."""
    table = boto3.resource("dynamodb").Table(TABLE)
    return UsageDay.from_item(table.get_item(Key={"github_id": 7, "day": day}, ConsistentRead=True)["Item"])


def test_fifty_reservations_from_parallel_threads_against_a_budget_that_fits_ten_admit_ten(client: Calls) -> None:
    ledger = DynamoDBLedger(client, TABLE, clock=Clock())
    who = student(budget=10 * 1500, limit=50)
    together = threading.Barrier(50)

    def one(number: int) -> Reservation | Cause:
        together.wait()
        try:
            return reserve(ledger, who, f"request-{number}")
        except GatewayError as refused:
            return refused.cause

    with ThreadPoolExecutor(50) as threads:
        answers = list(threads.map(one, range(50)))

    admitted = [answer for answer in answers if isinstance(answer, Reservation)]
    assert len(admitted) == 10
    assert answers.count(Cause.BUDGET_REACHED) == 40
    record = written()
    assert record.tokens == 15_000
    assert record.leases == {reservation.request_id: reservation.lease for reservation in admitted}
    assert ledger.tokens_left(who) == 0


def test_the_item_of_a_day_and_the_two_writes_of_a_request(client: Calls) -> None:
    ledger = DynamoDBLedger(client, TABLE, clock=Clock())
    who = student()
    assert ledger.tokens_left(who) == DAILY_BUDGET

    # The student's first request of the day writes the item whole.
    first = reserve(ledger, who, "request-1", estimate=1000, output=500)
    lease = Lease(tokens=1500, estimate=1000, expires=NOON + 2280)
    assert first == Reservation(github_id=7, day="2026-11-15", request_id="request-1", lease=lease)
    assert written() == UsageDay(
        github_id=7,
        day="2026-11-15",
        alias="student-07",
        tokens=1500,
        expires_at=COURSE_END + USAGE_RETENTION_SECONDS,
        leases={"request-1": lease},
    )

    # From then on a request is two calls, and each is a write. The read is a read.
    client.made.clear()
    second = reserve(ledger, who, "request-2", estimate=2000, output=500)
    assert ledger.settle(second, input_tokens=90_000, output_tokens=500, ended_cleanly=True) == 90_500
    assert client.made == ["update_item", "update_item"]
    assert ledger.tokens_left(who) == DAILY_BUDGET - 1500 - 90_500
    assert client.made == ["update_item", "update_item", "get_item"]

    # Any other end is charged what was observed, and never less than the estimate; and once.
    assert ledger.settle(first, input_tokens=0, output_tokens=100, ended_cleanly=False) == 1000
    assert ledger.settle(first, input_tokens=5000, output_tokens=500, ended_cleanly=True) is None
    record = written()
    assert (record.tokens, record.leases) == (90_500 + 1000, {})


def test_the_refusals_and_the_slot_an_end_frees(client: Calls) -> None:
    ledger = DynamoDBLedger(client, TABLE, clock=Clock())
    who = student(limit=4)
    held = [reserve(ledger, who, f"request-{number}") for number in range(4)]

    assert refusal(ledger, who, "request-4") is Cause.CONCURRENCY_LIMIT
    ledger.settle(held[0], input_tokens=1000, output_tokens=20, ended_cleanly=True)
    reserve(ledger, who, "request-5")
    assert refusal(ledger, who, "request-6") is Cause.CONCURRENCY_LIMIT

    spent = written().tokens
    assert refusal(ledger, student(budget=spent + 1499, limit=5), "request-7") is Cause.BUDGET_REACHED
    assert refusal(ledger, student(budget=spent + 1499, limit=4), "request-7") is Cause.BUDGET_REACHED
    reserve(ledger, student(budget=spent + 1500, limit=5), "request-7")
    assert len(written().leases) == 5
    with pytest.raises(ValueError, match="already"):
        reserve(ledger, who, "request-7")


def test_an_expired_lease_frees_its_slot_and_is_charged_its_estimate(client: Calls) -> None:
    clock = Clock()
    ledger = DynamoDBLedger(client, TABLE, clock=clock)
    who = student(limit=2)
    first = reserve(ledger, who, "request-1", estimate=1000, output=500)
    clock.now = NOON + 1
    second = reserve(ledger, who, "request-2", estimate=3000, output=500)

    clock.now = NOON + 2279
    assert refusal(ledger, who, "request-3") is Cause.CONCURRENCY_LIMIT
    assert ledger.tokens_left(who) == DAILY_BUDGET - 1500 - 3500

    # The first lease has expired and the second has not: the read counts the first at its
    # estimate, and the reservation that needs its slot removes it in its own write.
    clock.now = NOON + 2280
    assert ledger.tokens_left(who) == DAILY_BUDGET - 1000 - 3500
    client.made.clear()
    third = reserve(ledger, who, "request-3", estimate=1000, output=500)
    assert client.made == ["update_item", "update_item"]  # the one refused, then the one written
    record = written()
    assert (record.tokens, set(record.leases)) == (1000 + 3500 + 1500, {"request-2", "request-3"})
    assert ledger.settle(first, input_tokens=9000, output_tokens=500, ended_cleanly=True) is None

    # A settlement that comes after the expiry removes the lease and charges its estimate.
    clock.now = NOON + 2281
    assert ledger.settle(second, input_tokens=9000, output_tokens=500, ended_cleanly=True) == 3000
    assert ledger.settle(third, input_tokens=1000, output_tokens=50, ended_cleanly=True) == 1050
    record = written()
    assert (record.tokens, record.leases) == (1000 + 3000 + 1050, {})


def test_a_reservation_gives_up_on_a_record_that_keeps_changing_and_raises_any_other_error(client: Calls) -> None:
    who = student()
    reserve(DynamoDBLedger(client, TABLE, clock=Clock()), who, "request-1")
    room = {
        name: value for name, value in client.get_item(TableName=TABLE, Key=KEY)["Item"].items() if name != "leases"
    }

    class Changing:
        """A table whose every write finds that another request has just made room."""

        sent = 0

        def update_item(self, **arguments: Any) -> Any:
            self.sent += 1
            failed = {"Error": {"Code": "ConditionalCheckFailedException"}, "Item": {**room, "leases": {"M": {}}}}
            raise ClientError(failed, "UpdateItem")

    changing = Changing()
    with pytest.raises(GatewayError) as gave_up:
        reserve(DynamoDBLedger(changing, TABLE, clock=Clock()), who, "request-2")
    assert (gave_up.value.cause, changing.sent) == (Cause.CONCURRENCY_LIMIT, 10)

    # Any other error of DynamoDB is raised as it is, with no cause: the gateway renders `engine_error`.
    with pytest.raises(ClientError) as failed:
        reserve(DynamoDBLedger(client, "no-such-table", clock=Clock()), who, "request-3")
    assert cause_of(failed.value) is Cause.ENGINE_ERROR
    assert set(written().leases) == {"request-1"}
