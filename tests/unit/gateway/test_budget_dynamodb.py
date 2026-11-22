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
from rhi_gateway.budget import (
    BYTES_PER_TOKEN,
    USAGE_RETENTION_SECONDS,
    DynamoDBLedger,
    InMemoryLedger,
    Ledger,
    Reservation,
)
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
    ledger: Ledger,
    who: StudentKey,
    request: str,
    *,
    estimate: int = 1000,
    output: int = 500,
    pool: str | None = None,
) -> Reservation:
    return ledger.reserve(
        who,
        request,
        b"x" * (estimate * BYTES_PER_TOKEN),
        output_limit=output,
        hold_seconds=DEFAULT_HOLD_SECONDS,
        longest_request_seconds=LONGEST_REQUEST_SECONDS,
        pool=pool,
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


def test_the_two_writes_of_a_request_carry_the_cookie_of_its_pool(client: Calls) -> None:
    ledger = DynamoDBLedger(client, TABLE, clock=Clock())
    who = student()

    # The record holds no cookie: none is returned, the item reads as holding none, and the
    # settlement stores its cookie.
    first = reserve(ledger, who, "request-1", pool="coursework")
    assert first.cookie is None
    assert written().affinity_cookies == {}
    client.made.clear()
    ledger.settle(first, input_tokens=1000, output_tokens=20, ended_cleanly=True, cookie="coursework-1")
    assert client.made == ["update_item"]
    assert written().affinity_cookies == {"coursework": "coursework-1"}

    # From then on the reservation's one call returns the cookie, and the settlement's one call
    # replaces it, for the request's pool only.
    client.made.clear()
    second = reserve(ledger, who, "request-2", pool="coursework")
    other = reserve(ledger, who, "request-3", pool="upgrade")
    assert (second.cookie, other.cookie) == ("coursework-1", None)
    ledger.settle(other, input_tokens=1000, output_tokens=20, ended_cleanly=False, cookie="upgrade-1")
    ledger.settle(second, input_tokens=1000, output_tokens=20, ended_cleanly=True, cookie="coursework-2")
    assert client.made == ["update_item"] * 4
    assert written().affinity_cookies == {"coursework": "coursework-2", "upgrade": "upgrade-1"}

    # A request that ended with no cookie stores none, and a settlement that finds nothing to settle neither.
    third = reserve(ledger, who, "request-4", pool="upgrade")
    assert third.cookie == "upgrade-1"
    ledger.settle(third, input_tokens=0, output_tokens=0, ended_cleanly=False)
    assert ledger.settle(third, input_tokens=0, output_tokens=0, ended_cleanly=False, cookie="too-late") is None
    record = written()
    assert record.affinity_cookies == {"coursework": "coursework-2", "upgrade": "upgrade-1"}
    assert record.leases == {}


def test_the_first_cookies_of_requests_in_flight_each_cost_one_settlement_call(client: Calls) -> None:
    ledger = DynamoDBLedger(client, TABLE, clock=Clock())
    who = student()
    # A day's item that no reservation naming a pool has written holds no map of cookies.
    ledger.settle(reserve(ledger, who, "request-0"), input_tokens=0, output_tokens=0, ended_cleanly=True)
    assert "affinity_cookies" not in client.get_item(TableName=TABLE, Key=KEY)["Item"]

    # Three reservations find a record with no cookie, two of them for one pool.
    client.made.clear()
    one = reserve(ledger, who, "request-1", pool="coursework")
    two = reserve(ledger, who, "request-2", pool="upgrade")
    three = reserve(ledger, who, "request-3", pool="coursework")
    assert (one.cookie, two.cookie, three.cookie) == (None, None, None)
    assert client.made == ["update_item"] * 3

    # Each settlement is one call, whatever the others stored meanwhile, and no cookie is lost.
    client.made.clear()
    assert ledger.settle(one, input_tokens=1000, output_tokens=20, ended_cleanly=True, cookie="coursework-1") == 1020
    assert ledger.settle(two, input_tokens=1000, output_tokens=30, ended_cleanly=True, cookie="upgrade-1") == 1030
    assert written().affinity_cookies == {"coursework": "coursework-1", "upgrade": "upgrade-1"}
    assert ledger.settle(three, input_tokens=1000, output_tokens=40, ended_cleanly=True, cookie="coursework-2") == 1040
    assert client.made == ["update_item"] * 3
    record = written()
    assert record.affinity_cookies == {"coursework": "coursework-2", "upgrade": "upgrade-1"}
    assert (record.tokens, record.leases) == (3090, {})


def test_the_first_cookies_of_two_requests_in_flight_are_both_kept(client: Calls) -> None:
    ledger = DynamoDBLedger(client, TABLE, clock=Clock())
    who = student()
    # The first reservation writes the day's item whole, and the second adds to it.
    one = reserve(ledger, who, "request-1", pool="coursework")
    two = reserve(ledger, who, "request-2", pool="upgrade")

    client.made.clear()
    ledger.settle(two, input_tokens=1000, output_tokens=30, ended_cleanly=True, cookie="upgrade-1")
    ledger.settle(one, input_tokens=1000, output_tokens=20, ended_cleanly=True, cookie="coursework-1")
    assert client.made == ["update_item"] * 2
    assert written().affinity_cookies == {"coursework": "coursework-1", "upgrade": "upgrade-1"}


def test_the_first_request_of_a_day_returns_the_cookie_of_the_day_before(client: Calls) -> None:
    clock = Clock()
    ledger = DynamoDBLedger(client, TABLE, clock=clock)
    who = student()
    first = reserve(ledger, who, "request-1", pool="coursework")
    ledger.settle(first, input_tokens=1000, output_tokens=20, ended_cleanly=True, cookie="coursework-1")

    clock.now = NOON + 86_400
    assert reserve(ledger, who, "request-2", pool="coursework").cookie == "coursework-1"
    assert written("2026-11-16").affinity_cookies == {"coursework": "coursework-1"}


def test_the_two_writes_of_a_request_carry_the_last_rebalance_number_of_its_pool(client: Calls) -> None:
    clock = Clock()
    ledger = DynamoDBLedger(client, TABLE, clock=clock)
    who = student()

    def settle(reservation: Reservation, **affinity: Any) -> int | None:
        return ledger.settle(reservation, input_tokens=1000, output_tokens=20, ended_cleanly=True, **affinity)

    # The record has applied none: none is returned, and the settlement's one call stores the
    # number with the cookie.
    first = reserve(ledger, who, "request-1", pool="coursework")
    assert first.rebalance_applied is None
    assert written().rebalance_applied == {}
    client.made.clear()
    settle(first, cookie="coursework-1", rebalance_number=7)
    assert client.made == ["update_item"]
    assert written().rebalance_applied == {"coursework": 7}

    # From then on the reservation's one call returns it, for the request's pool only, and the
    # settlement's one call replaces it, with a cookie or without, whatever else is in flight.
    client.made.clear()
    second = reserve(ledger, who, "request-2", pool="coursework")
    other = reserve(ledger, who, "request-3", pool="upgrade")
    assert (second.cookie, second.rebalance_applied) == ("coursework-1", 7)
    assert (other.cookie, other.rebalance_applied) == (None, None)
    settle(other, rebalance_number=0)
    settle(second, cookie="coursework-2", rebalance_number=8)
    assert client.made == ["update_item"] * 4
    record = written()
    assert record.rebalance_applied == {"coursework": 8, "upgrade": 0}
    assert record.affinity_cookies == {"coursework": "coursework-2"}

    # A request that was not judged stores no number. One whose cookie was dropped and that ended
    # with none takes the pool's cookie off the record, in its one call, and stores its number.
    settle(reserve(ledger, who, "request-4", pool="coursework"), cookie="coursework-3")
    assert written().rebalance_applied["coursework"] == 8
    dropped = reserve(ledger, who, "request-5", pool="coursework")
    client.made.clear()
    settle(dropped, rebalance_number=9, cookie_dropped=True)
    assert client.made == ["update_item"]
    record = written()
    assert (record.affinity_cookies, record.rebalance_applied) == ({}, {"coursework": 9, "upgrade": 0})
    # A settlement that finds nothing to settle stores nothing.
    assert settle(dropped, cookie="too-late", rebalance_number=10) is None
    assert written().rebalance_applied["coursework"] == 9

    # The first request of the next day returns the number of the day before.
    clock.now = NOON + 86_400
    assert reserve(ledger, who, "request-6", pool="coursework").rebalance_applied == 9
    assert written("2026-11-16").rebalance_applied == {"coursework": 9, "upgrade": 0}


def test_the_first_numbers_of_requests_in_flight_each_cost_one_settlement_call(client: Calls) -> None:
    ledger = DynamoDBLedger(client, TABLE, clock=Clock())
    who = student()
    # A day's item that no reservation naming a pool has written holds no map of numbers.
    ledger.settle(reserve(ledger, who, "request-0"), input_tokens=0, output_tokens=0, ended_cleanly=True)
    assert "rebalance_applied" not in client.get_item(TableName=TABLE, Key=KEY)["Item"]

    one = reserve(ledger, who, "request-1", pool="coursework")
    two = reserve(ledger, who, "request-2", pool="upgrade")
    client.made.clear()
    ledger.settle(two, input_tokens=1000, output_tokens=30, ended_cleanly=True, rebalance_number=3)
    ledger.settle(one, input_tokens=1000, output_tokens=20, ended_cleanly=True, cookie="held", rebalance_number=7)
    assert client.made == ["update_item"] * 2
    record = written()
    assert (record.rebalance_applied, record.leases) == ({"coursework": 7, "upgrade": 3}, {})


def test_a_withdrawal_deletes_the_day_only_when_nothing_else_is_left_in_it(client: Calls) -> None:
    ledger = DynamoDBLedger(client, TABLE, clock=Clock())
    ledger.withdraw(reserve(ledger, student(), "alone", pool="coursework"))
    assert "Item" not in client.get_item(TableName=TABLE, Key=KEY, ConsistentRead=True)

    settled = reserve(ledger, student(), "settled")
    assert ledger.settle(settled, input_tokens=900, output_tokens=100, ended_cleanly=True) == 1000
    ledger.withdraw(reserve(ledger, student(), "withdrawn"))
    assert (written().tokens, written().leases) == (1000, {})


@pytest.mark.parametrize("kind", ["memory", "dynamodb"])
def test_a_withdrawal_keeps_what_a_settlement_stored_even_at_no_tokens(client: Calls, kind: str) -> None:
    ledger = InMemoryLedger(clock=Clock()) if kind == "memory" else DynamoDBLedger(client, TABLE, clock=Clock())
    completed = reserve(ledger, student(), "completed", pool="coursework")
    settled = ledger.settle(
        completed, input_tokens=0, output_tokens=0, ended_cleanly=True, cookie="kept-cookie", rebalance_number=7
    )
    assert settled == 0

    ledger.withdraw(reserve(ledger, student(), "withdrawn", pool="coursework"))

    following = reserve(ledger, student(), "following", pool="coursework")
    assert (following.cookie, following.rebalance_applied) == ("kept-cookie", 7)


def test_withdrawals_delete_a_day_left_with_only_what_its_opening_carried(
    client: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = DynamoDBLedger(client, TABLE, clock=Clock())
    table = boto3.resource("dynamodb").Table(TABLE)
    before = UsageDay(
        7,
        "2026-11-14",
        "student-07",
        tokens=5,
        expires_at=COURSE_END,
        leases={"running": Lease(tokens=10, estimate=10, expires=NOON + 600)},
        affinity_cookies={"coursework": "AWSALB=cookie-of-yesterday"},
        rebalance_applied={"coursework": 3},
    )
    deletes: list[dict[str, Any]] = []
    delete = client.delete_item
    monkeypatch.setattr(client, "delete_item", lambda **request: deletes.append(request) or delete(**request))
    for order in (["opener"], ["opener", "other"], ["other", "opener"]):
        table.put_item(Item=before.to_item())
        held = {"opener": reserve(ledger, student(), "opener", pool="coursework")}
        # The day before goes once the opening has read it, as the admin's erase deletes it.
        table.delete_item(Key={"github_id": 7, "day": "2026-11-14"})
        if "other" in order:
            held["other"] = reserve(ledger, student(), "other", pool="coursework")
        for request in order[:-1]:
            ledger.withdraw(held[request])
            # What another request holds keeps the day.
            assert set(written().leases) == {"2026-11-14/running", order[-1]}
        ledger.withdraw(held[order[-1]])
        # The stand-in, the cookie and the number carried from the day before go with the last.
        assert "Item" not in client.get_item(TableName=TABLE, Key=KEY, ConsistentRead=True)
    # Every delete binds only the values its condition names, which DynamoDB requires.
    assert deletes
    for request in deletes:
        assert all(name in request["ConditionExpression"] for name in request["ExpressionAttributeValues"])


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


class Paused:
    """A client whose first call of one operation waits for the test, before it reaches the table.

    With `after`, it also waits once that call has been applied, before it answers its caller.
    """

    def __init__(self, client: Calls, operation: str, *, after: bool = False) -> None:
        self._client = client
        self._operation = operation
        self._after = after
        self.reached = threading.Event()
        self.go = threading.Event()
        self.applied = threading.Event()
        self.answer = threading.Event()

    def __getattr__(self, operation: str) -> Any:
        call = getattr(self._client, operation)

        def paused(**arguments: Any) -> Any:
            if operation != self._operation or self.reached.is_set():
                return call(**arguments)
            self.reached.set()
            assert self.go.wait(timeout=10)
            result = call(**arguments)
            self.applied.set()
            if self._after:
                assert self.answer.wait(timeout=10)
            return result

        return paused

    def release(self) -> None:
        self.go.set()
        self.answer.set()


def test_the_opening_of_a_day_admits_no_more_than_the_limit_across_midnight(client: Calls) -> None:
    midnight = NOON + 12 * 3600
    clock = Clock(midnight - 60)
    who = student(limit=4)

    def answer(ledger: DynamoDBLedger, request: str) -> Reservation | Cause:
        try:
            return reserve(ledger, who, request)
        except GatewayError as refused:
            return refused.cause

    ordinary = DynamoDBLedger(client, TABLE, clock=clock)
    running = [answer(ordinary, f"running-{number}") for number in range(2)]
    late = [Paused(client, "update_item") for _ in range(2)]
    opening = Paused(client, "put_item", after=True)
    with ThreadPoolExecutor(3) as threads:
        try:
            # Two reservations of the old day have reached the table but are not yet applied.
            clock.now = midnight - 1
            pending = [
                threads.submit(answer, DynamoDBLedger(paused, TABLE, clock=clock), f"late-{number}")
                for number, paused in enumerate(late)
            ]
            for paused in late:
                assert paused.reached.wait(timeout=10)
            # The first reservation of the new day has read the old day and is about to write its item.
            clock.now = midnight + 1
            opener = threads.submit(answer, DynamoDBLedger(opening, TABLE, clock=clock), "opener")
            assert opening.reached.wait(timeout=10)
            for paused in late:
                paused.release()
            answers = [one.result(timeout=10) for one in pending]
            # The new day's item is written, and its opener has not yet answered: a follower comes now.
            opening.go.set()
            assert opening.applied.wait(timeout=10)
            follower = answer(ordinary, "follower")
            opening.answer.set()
            answers += [follower, opener.result(timeout=10)]
        finally:
            for paused in [*late, opening]:
                paused.release()

    answers += running
    assert sum(isinstance(one, Reservation) for one in answers) <= who.concurrency_limit, answers
    assert answers.count(Cause.CONCURRENCY_LIMIT) >= 2, answers


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
