"""Reserve budget before a request, settle after it (`rhi_gateway.budget`; section 6.2, step 3).

These tests run the in-memory ledger, on a clock they control. The DynamoDB ledger decides with
the same functions and is run against moto in `test_budget_dynamodb.py`.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from rhi_common import retention
from rhi_common.causes import Cause
from rhi_common.pool_config import DEFAULT_HOLD_SECONDS
from rhi_common.students import Lease, StudentKey, UsageDay
from rhi_gateway.budget import (
    BYTES_PER_TOKEN,
    InMemoryLedger,
    Reservation,
    estimate_input_tokens,
    lease_seconds,
)
from rhi_gateway.errors import GatewayError
from rhi_gateway.usage.messages import read_answer

# The defaults of section 17: 100 million tokens a day, and a longest request of 30 minutes.
DAILY_BUDGET = 100_000_000
LONGEST_REQUEST_SECONDS = 1800
NOON = int(datetime(2026, 11, 15, 12, tzinfo=UTC).timestamp())
COURSE_END = NOON + 30 * 86_400
# The same course end as a date, which the ledger is given.
COURSE_END_DATE = datetime.fromtimestamp(COURSE_END, UTC).date()


class Clock:
    def __init__(self, now: int = NOON) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


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
    ledger: InMemoryLedger,
    who: StudentKey,
    request: str,
    *,
    estimate: int = 1000,
    output: int = 500,
    pool: str | None = None,
) -> Reservation:
    """Reserve for a request whose body is estimated at `estimate` tokens, with the defaults of section 17."""
    return ledger.reserve(
        who,
        request,
        b"x" * (estimate * BYTES_PER_TOKEN),
        output_limit=output,
        hold_seconds=DEFAULT_HOLD_SECONDS,
        longest_request_seconds=LONGEST_REQUEST_SECONDS,
        pool=pool,
    )


def settle(
    ledger: InMemoryLedger,
    reservation: Reservation,
    *,
    cookie: str | None = None,
    rebalance_number: int | None = None,
    cookie_dropped: bool = False,
) -> int | None:
    """Settle a clean end of 1,000 input tokens and 200 output tokens, with what the request's affinity gives."""
    return ledger.settle(
        reservation,
        input_tokens=1000,
        output_tokens=200,
        ended_cleanly=True,
        cookie=cookie,
        rebalance_number=rebalance_number,
        cookie_dropped=cookie_dropped,
    )


def refusal(ledger: InMemoryLedger, who: StudentKey, request: str) -> Cause:
    """The cause a reservation is refused with."""
    with pytest.raises(GatewayError) as refused:
        reserve(ledger, who, request)
    return refused.value.cause


def written(ledger: InMemoryLedger, day: str = "2026-11-15") -> UsageDay:
    """The student's day as the record holds it."""
    record = ledger.day(7, day)
    assert record is not None
    return record


def charged(ledger: InMemoryLedger, day: str = "2026-11-15") -> int:
    return written(ledger, day).tokens


def test_the_estimate_is_computed_from_the_body_alone() -> None:
    assert [estimate_input_tokens(b"x" * size) for size in (0, 1, 4, 5, 400)] == [0, 1, 1, 2, 100]
    assert lease_seconds(DEFAULT_HOLD_SECONDS, LONGEST_REQUEST_SECONDS) == 2280


def test_a_reservation_adds_the_estimate_and_the_output_limit_and_takes_a_slot() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student()

    reservation = reserve(ledger, who, "request-1", estimate=1000, output=500)

    lease = Lease(tokens=1500, estimate=1000, expires=NOON + 2280)
    assert reservation == Reservation(github_id=7, day="2026-11-15", request_id="request-1", lease=lease)
    record = written(ledger)
    assert (record.alias, record.tokens, record.leases) == ("student-07", 1500, {"request-1": lease})
    assert record.expires_at == retention.expires_at(COURSE_END_DATE)
    assert ledger.tokens_left(who) == DAILY_BUDGET - 1500


def test_fifty_reservations_at_once_against_a_budget_that_fits_ten_admit_ten() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
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

    assert sum(isinstance(answer, Reservation) for answer in answers) == 10
    assert answers.count(Cause.BUDGET_REACHED) == 40
    record = written(ledger)
    assert (record.tokens, len(record.leases)) == (15_000, 10)
    assert ledger.tokens_left(who) == 0


def test_a_fifth_concurrent_request_is_refused_until_one_has_ended() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student(limit=4)
    held = [reserve(ledger, who, f"request-{number}") for number in range(4)]

    assert refusal(ledger, who, "request-4") is Cause.CONCURRENCY_LIMIT
    assert ledger.writes == 4

    # Either end frees the slot.
    ledger.settle(held[0], input_tokens=1000, output_tokens=20, ended_cleanly=True)
    reserve(ledger, who, "request-5")
    assert refusal(ledger, who, "request-6") is Cause.CONCURRENCY_LIMIT
    ledger.settle(held[1], input_tokens=0, output_tokens=0, ended_cleanly=False)
    reserve(ledger, who, "request-6")
    assert len(written(ledger).leases) == 4
    # A student over the budget and at the limit is told the one no retry cures.
    assert refusal(ledger, student(budget=0, limit=4), "request-7") is Cause.BUDGET_REACHED


def test_a_clean_end_replaces_the_reservation_by_the_reported_usage() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student()
    reservation = reserve(ledger, who, "request-1", estimate=1000, output=500)

    # Less than the estimate, and no floor: the engine's count replaces the estimate.
    assert ledger.settle(reservation, input_tokens=700, output_tokens=40, ended_cleanly=True) == 740
    assert charged(ledger) == 740
    assert ledger.tokens_left(who) == DAILY_BUDGET - 740


def test_hanging_up_early_is_charged() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student()
    reservation = reserve(ledger, who, "request-1", estimate=1000, output=500)

    # The client hangs up when 100 tokens have been observed.
    assert ledger.settle(reservation, input_tokens=0, output_tokens=100, ended_cleanly=False) == 1000
    assert charged(ledger) == 1000
    assert ledger.tokens_left(who) == DAILY_BUDGET - 1000

    # A second settlement charges nothing, whatever it reports.
    assert ledger.settle(reservation, input_tokens=5000, output_tokens=500, ended_cleanly=True) is None
    assert charged(ledger) == 1000
    assert ledger.tokens_left(who) == DAILY_BUDGET - 1000


def test_an_end_that_is_not_clean_charges_what_was_observed_above_the_estimate() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    reservation = reserve(ledger, student(), "request-1", estimate=1000, output=500)

    assert ledger.settle(reservation, input_tokens=1200, output_tokens=300, ended_cleanly=False) == 1500
    assert charged(ledger) == 1500


def test_cached_input_is_charged() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student()
    before = ledger.tokens_left(who)
    assert before == DAILY_BUDGET
    reservation = reserve(ledger, who, "request-1", estimate=85_000, output=4000)

    # 90,000 input tokens, of which 80,000 were read from the cache, and 500 output tokens.
    usage = read_answer(
        json.dumps({"usage": {"input_tokens": 10_000, "cache_read_input_tokens": 80_000, "output_tokens": 500}})
    )
    assert (usage.input_tokens, usage.output_tokens, usage.ended_cleanly) == (90_000, 500, True)
    settled = ledger.settle(
        reservation,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        ended_cleanly=usage.ended_cleanly,
    )

    assert settled == 90_500
    assert charged(ledger) == 90_500
    assert before - ledger.tokens_left(who) == 90_500


def test_two_writes_per_request() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student()

    reservation = reserve(ledger, who, "request-1", pool="coursework")
    assert (reservation.cookie, reservation.rebalance_applied) == (None, None)
    assert ledger.writes == 1
    settle(ledger, reservation, cookie="first-cookie", rebalance_number=7)
    assert ledger.writes == 2

    # The read writes nothing, and neither does a settlement that finds nothing to settle.
    ledger.tokens_left(who)
    settle(ledger, reservation, cookie="late-cookie", rebalance_number=9)
    assert ledger.writes == 2

    # The cookie and the last rebalance number applied are carried by those two writes: the next
    # request's reservation returns them, and that request makes two writes too.
    second = reserve(ledger, who, "request-2", pool="coursework")
    assert (second.cookie, second.rebalance_applied) == ("first-cookie", 7)
    assert ledger.writes == 3
    settle(ledger, second, cookie="second-cookie", rebalance_number=8)
    assert ledger.writes == 4
    record = written(ledger)
    assert (record.affinity_cookies, record.rebalance_applied) == ({"coursework": "second-cookie"}, {"coursework": 8})


def test_a_cookie_is_kept_per_pool_and_only_a_settlement_stores_one() -> None:
    clock = Clock()
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=clock)
    who = student(limit=2)

    def cookies() -> dict[str, str]:
        day = ledger.day(7, "2026-11-15")
        assert day is not None
        return day.affinity_cookies

    first = reserve(ledger, who, "request-1", pool="coursework")
    assert first.cookie is None
    assert cookies() == {}
    ledger.settle(first, input_tokens=1000, output_tokens=20, ended_cleanly=True, cookie="coursework-cookie")

    # Another pool's reservation returns none of it, and its settlement leaves it in place.
    other = reserve(ledger, who, "request-2", pool="upgrade")
    assert other.cookie is None
    ledger.settle(other, input_tokens=1000, output_tokens=20, ended_cleanly=False, cookie="upgrade-cookie")
    assert cookies() == {"coursework": "coursework-cookie", "upgrade": "upgrade-cookie"}
    assert reserve(ledger, who, "request-3", pool="upgrade").cookie == "upgrade-cookie"

    # A request that ended with none stores none, and removes none.
    fourth = reserve(ledger, who, "request-4", pool="coursework")
    assert fourth.cookie == "coursework-cookie"
    ledger.settle(fourth, input_tokens=0, output_tokens=0, ended_cleanly=False, cookie=None)
    assert cookies() == {"coursework": "coursework-cookie", "upgrade": "upgrade-cookie"}

    # The reservation that removes an expired lease leaves the cookies as they are, and so does
    # the settlement that then finds its lease gone.
    fifth = reserve(ledger, who, "request-5", pool="coursework")
    clock.now = NOON + 2280
    reserve(ledger, who, "request-6", pool="coursework")
    assert ledger.settle(fifth, input_tokens=1, output_tokens=1, ended_cleanly=True, cookie="too-late") is None
    assert cookies() == {"coursework": "coursework-cookie", "upgrade": "upgrade-cookie"}


def test_a_new_day_starts_with_the_cookies_and_the_rebalance_numbers_of_the_day_before() -> None:
    clock = Clock()
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=clock)
    who = student()
    first = reserve(ledger, who, "request-1", pool="coursework")
    settle(ledger, first, cookie="coursework-cookie", rebalance_number=7)

    clock.now = NOON + 86_400
    second = reserve(ledger, who, "request-2", pool="coursework")
    assert (second.cookie, second.rebalance_applied) == ("coursework-cookie", 7)
    today = ledger.day(7, "2026-11-16")
    assert today is not None
    assert today.affinity_cookies == {"coursework": "coursework-cookie"}
    assert today.rebalance_applied == {"coursework": 7}


def test_a_rebalance_number_is_kept_per_pool_and_only_a_settlement_stores_one() -> None:
    clock = Clock()
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=clock)
    who = student(limit=1)
    settle(ledger, reserve(ledger, who, "request-1", pool="coursework"), cookie="held", rebalance_number=7)

    # Another pool's reservation returns none of it, and its settlement leaves it in place.
    other = reserve(ledger, who, "request-2", pool="upgrade")
    assert other.rebalance_applied is None
    settle(ledger, other, rebalance_number=0)
    assert written(ledger).rebalance_applied == {"coursework": 7, "upgrade": 0}

    # A request that was not judged stores no number, and one that ended with no cookie stores its number still.
    settle(ledger, reserve(ledger, who, "request-3", pool="coursework"), cookie="newer")
    assert written(ledger).rebalance_applied["coursework"] == 7
    settle(ledger, reserve(ledger, who, "request-4", pool="coursework"), rebalance_number=8)
    record = written(ledger)
    assert (record.affinity_cookies, record.rebalance_applied["coursework"]) == ({"coursework": "newer"}, 8)

    # A request whose cookie was dropped, and that ended with none, takes the pool's cookie off the record.
    settle(ledger, reserve(ledger, who, "request-5", pool="coursework"), rebalance_number=9, cookie_dropped=True)
    record = written(ledger)
    assert (record.affinity_cookies, record.rebalance_applied) == ({}, {"coursework": 9, "upgrade": 0})
    # One that ended with the cookie its answer set stores that cookie.
    moved = reserve(ledger, who, "request-6", pool="coursework")
    settle(ledger, moved, cookie="moved", rebalance_number=10, cookie_dropped=True)
    assert written(ledger).affinity_cookies == {"coursework": "moved"}

    # The reservation that removes an expired lease stores nothing, and neither does the
    # settlement that then finds its lease gone.
    late = reserve(ledger, who, "request-7", pool="coursework")
    clock.now = NOON + 2280
    assert reserve(ledger, who, "request-8", pool="coursework").rebalance_applied == 10
    assert settle(ledger, late, rebalance_number=11, cookie_dropped=True) is None
    record = written(ledger)
    assert (record.affinity_cookies, record.rebalance_applied["coursework"]) == ({"coursework": "moved"}, 10)


def test_a_lease_expires_after_2280_seconds() -> None:
    clock = Clock()
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=clock)
    who = student(limit=1)
    first = reserve(ledger, who, "request-1", estimate=1000, output=500)
    assert first.lease.expires - NOON == lease_seconds(DEFAULT_HOLD_SECONDS, LONGEST_REQUEST_SECONDS) == 2280

    clock.now = NOON + 2279
    assert refusal(ledger, who, "request-2") is Cause.CONCURRENCY_LIMIT
    assert ledger.tokens_left(who) == DAILY_BUDGET - 1500

    # Expired, the lease holds no slot and is worth its estimate, before any write removes it.
    clock.now = NOON + 2281
    assert ledger.tokens_left(who) == DAILY_BUDGET - 1000
    reserve(ledger, who, "request-2", estimate=1000, output=500)
    record = written(ledger)
    assert (record.tokens, set(record.leases)) == (1000 + 1500, {"request-2"})

    # The request of the expired lease ends at last: it was settled at its expiry, and is not again.
    assert ledger.settle(first, input_tokens=9000, output_tokens=500, ended_cleanly=True) is None
    assert charged(ledger) == 1000 + 1500


def test_a_settlement_after_the_expiry_charges_the_estimate() -> None:
    clock = Clock()
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=clock)
    who = student()
    reservation = reserve(ledger, who, "request-1", estimate=1000, output=500)

    clock.now = NOON + 2280
    assert ledger.settle(reservation, input_tokens=9000, output_tokens=500, ended_cleanly=True) == 1000
    record = written(ledger)
    assert (record.tokens, record.leases) == (1000, {})


def test_a_request_belongs_to_the_utc_day_it_started() -> None:
    clock = Clock(NOON + 12 * 3600 - 30)  # 23:59:30 UTC
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=clock)
    who = student()
    reservation = reserve(ledger, who, "request-1", estimate=1000, output=500)
    assert reservation.day == "2026-11-15"

    clock.now += 60
    assert ledger.tokens_left(who) == DAILY_BUDGET
    assert ledger.settle(reservation, input_tokens=1000, output_tokens=100, ended_cleanly=True) == 1100
    assert charged(ledger, "2026-11-15") == 1100
    assert ledger.day(7, "2026-11-16") is None
    assert ledger.tokens_left(who) == DAILY_BUDGET


def test_a_request_id_reserves_once() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student()
    reserve(ledger, who, "request-1")

    with pytest.raises(ValueError, match="already"):
        reserve(ledger, who, "request-1")
    assert ledger.writes == 1
