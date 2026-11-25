"""Take a concurrency slot before a request, release it after (`rhi_gateway.budget`; section 6.2, step 3).

These tests run the in-memory ledger, on a clock they control. The DynamoDB ledger decides with
the same functions and is run against moto in `test_budget_dynamodb.py`. No budget takes part in a
reservation: a key has none.
"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from rhi_common import retention
from rhi_common.causes import Cause
from rhi_common.students import Lease, StudentKey, UsageDay
from rhi_gateway.budget import InMemoryLedger, Reservation, lease_seconds
from rhi_gateway.errors import GatewayError

# The longest request of section 17: 30 minutes.
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


def student(*, limit: int = 4) -> StudentKey:
    return StudentKey(
        key_hash=hashlib.sha256(b"not a key").hexdigest(),
        github_id=7,
        alias="student-07",
        installation=None,
        issued_at=NOON - 86_400,
        expires_at=COURSE_END,
        revoked_at=None,
        concurrency_limit=limit,
    )


def reserve(ledger: InMemoryLedger, who: StudentKey, request: str, *, pool: str | None = None) -> Reservation:
    """Take a slot for a request, with the defaults of section 17."""
    return ledger.reserve(
        who,
        request,
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
) -> bool:
    """Settle a request, with what its affinity gives."""
    return ledger.settle(reservation, cookie=cookie, rebalance_number=rebalance_number, cookie_dropped=cookie_dropped)


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


def test_a_reservation_takes_a_slot_and_writes_a_lease_of_its_expiry() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())

    reservation = reserve(ledger, student(), "request-1")

    lease = Lease(expires=NOON + 1860)
    assert reservation == Reservation(github_id=7, day="2026-11-15", request_id="request-1", lease=lease)
    record = written(ledger)
    assert (record.alias, record.leases) == ("student-07", {"request-1": lease})
    assert record.expires_at == retention.expires_at(COURSE_END_DATE)


def test_fifty_reservations_at_once_against_a_limit_of_ten_admit_ten() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student(limit=10)
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
    assert answers.count(Cause.CONCURRENCY_LIMIT) == 40
    record = written(ledger)
    assert len(record.leases) == 10


def test_a_fifth_concurrent_request_is_refused_until_one_has_ended() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student(limit=4)
    held = [reserve(ledger, who, f"request-{number}") for number in range(4)]

    assert refusal(ledger, who, "request-4") is Cause.CONCURRENCY_LIMIT
    assert ledger.writes == 4

    # Each end frees the slot, and charges nothing.
    assert settle(ledger, held[0]) is True
    assert len(written(ledger).leases) == 3
    reserve(ledger, who, "request-5")
    assert refusal(ledger, who, "request-6") is Cause.CONCURRENCY_LIMIT
    settle(ledger, held[1])
    reserve(ledger, who, "request-6")
    record = written(ledger)
    assert len(record.leases) == 4


def test_a_reservation_is_settled_once() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    reservation = reserve(ledger, student(), "request-1")

    assert settle(ledger, reservation) is True
    assert ledger.writes == 2
    # A second settlement finds nothing to settle, and writes nothing.
    assert settle(ledger, reservation) is False
    assert ledger.writes == 2
    record = written(ledger)
    assert record.leases == {}


def test_two_writes_per_request() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student()

    reservation = reserve(ledger, who, "request-1", pool="coursework")
    assert (reservation.cookie, reservation.rebalance_applied) == (None, None)
    assert ledger.writes == 1
    settle(ledger, reservation, cookie="first-cookie", rebalance_number=7)
    assert ledger.writes == 2

    # A settlement that finds nothing to settle writes nothing.
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
    ledger.settle(first, cookie="coursework-cookie")

    # Another pool's reservation returns none of it, and its settlement leaves it in place.
    other = reserve(ledger, who, "request-2", pool="upgrade")
    assert other.cookie is None
    ledger.settle(other, cookie="upgrade-cookie")
    assert cookies() == {"coursework": "coursework-cookie", "upgrade": "upgrade-cookie"}
    assert reserve(ledger, who, "request-3", pool="upgrade").cookie == "upgrade-cookie"

    # A request that ended with none stores none, and removes none.
    fourth = reserve(ledger, who, "request-4", pool="coursework")
    assert fourth.cookie == "coursework-cookie"
    ledger.settle(fourth, cookie=None)
    assert cookies() == {"coursework": "coursework-cookie", "upgrade": "upgrade-cookie"}

    # The reservation that removes an expired lease leaves the cookies as they are, and so does
    # the settlement that then finds its lease gone.
    fifth = reserve(ledger, who, "request-5", pool="coursework")
    clock.now = NOON + 1860
    reserve(ledger, who, "request-6", pool="coursework")
    assert ledger.settle(fifth, cookie="too-late") is False
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
    clock.now = NOON + 1860
    assert reserve(ledger, who, "request-8", pool="coursework").rebalance_applied == 10
    assert settle(ledger, late, rebalance_number=11, cookie_dropped=True) is False
    record = written(ledger)
    assert (record.affinity_cookies, record.rebalance_applied["coursework"]) == ({"coursework": "moved"}, 10)


def test_a_lease_expires_after_1860_seconds() -> None:
    clock = Clock()
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=clock)
    who = student(limit=1)
    first = reserve(ledger, who, "request-1")
    assert first.lease.expires - NOON == lease_seconds(LONGEST_REQUEST_SECONDS) == 1860

    clock.now = NOON + 1859
    assert refusal(ledger, who, "request-2") is Cause.CONCURRENCY_LIMIT

    # Expired, the lease holds no slot: the reservation that needs it removes it in its own write.
    clock.now = NOON + 1861
    reserve(ledger, who, "request-2")
    record = written(ledger)
    assert set(record.leases) == {"request-2"}

    # The request of the expired lease ends at last: its lease is gone, and nothing is written.
    assert settle(ledger, first) is False
    assert ledger.writes == 2


def test_a_settlement_after_the_expiry_removes_the_lease() -> None:
    clock = Clock()
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=clock)
    reservation = reserve(ledger, student(), "request-1")

    clock.now = NOON + 1860
    assert settle(ledger, reservation) is True
    record = written(ledger)
    assert record.leases == {}


def test_a_request_belongs_to_the_utc_day_it_started() -> None:
    clock = Clock(NOON + 12 * 3600 - 30)  # 23:59:30 UTC
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=clock)
    reservation = reserve(ledger, student(), "request-1")
    assert reservation.day == "2026-11-15"

    clock.now += 60
    assert settle(ledger, reservation) is True
    assert written(ledger, "2026-11-15").leases == {}
    assert ledger.day(7, "2026-11-16") is None


def test_a_request_id_reserves_once() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=Clock())
    who = student()
    reserve(ledger, who, "request-1")

    with pytest.raises(ValueError, match="already"):
        reserve(ledger, who, "request-1")
    assert ledger.writes == 1
