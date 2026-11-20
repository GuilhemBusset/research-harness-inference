"""Reserve budget before a request, settle after it (system design, section 6.2, step 3).

Checking a budget only at admission, or counting tokens only at the end, lets a client overspend
by opening many streams or by hanging up early. So a request writes its student's record twice,
and this module is the one place where it does:

1. **The reservation**, before anything is sent to the engine. One conditional write adds the
   estimate of the input plus the request's output limit to the tokens of the student's day, and
   adds a lease under the request's ID. Its condition is the budget and the limit themselves: the
   day's tokens and this amount fit the daily budget, and the leases are fewer than the
   concurrency limit. Both numbers are the ones the key's lookup gave (`Student`). Requests
   sent at once therefore cannot be admitted beyond either. A refusal is a `GatewayError`: for the
   budget, with the cause `budget_reached`; for the limit, with `concurrency_limit`. A student
   over both is told `budget_reached`, the one no retry cures.
2. **The settlement**, when the request has ended. One conditional write removes the lease and
   replaces the tokens it reserved by the charge. A clean end is charged the usage the engine
   reported, input plus output, the input counted whole, cached or not (sections 11.3 and 17). Any
   other end (a disconnect, a timeout, an error) is charged the tokens observed, and never less
   than the input estimate. Its condition is that the lease is still there, so a reservation is
   settled once: a second settlement writes nothing and charges nothing.

A write whose condition fails changes nothing and is not a write to the record. The ledger also
reads the tokens a student has left of the day (`tokens_left`), and that writes nothing.

**The record** is the usage item of `rhi_common.students`, one per student and UTC day: `tokens`
holds what was charged and what is reserved, `leases` the requests in flight. A request belongs
to the day it started: its lease stays in that day's item, and its settlement goes there even
after midnight. The item expires 90 days after the key's course end, the retention of every store
(section 6.6). In the table, an item can also carry `opening`, for a minute after midnight at most
(see "Midnight"); the record leaves it out.

**Midnight.** The budget is the day's and starts again at midnight UTC; the concurrency limit is
the student's and does not. So the first reservation of a day reads the item of the day before
and writes the new day's item with a stand-in for each lease still live there: a lease under the
day and the request's ID (`2026-11-15/<request>`) that reserves no tokens and expires when the
request's own lease does. The reservation's condition counts it like any lease, so a request
still running at midnight keeps its slot in the new day, at no extra call for the requests that
follow. Its settlement is still one write, to the item of its own day, which leaves the stand-in
behind: a reservation that stand-ins refuse reads the day before again and removes, in its own
single write, those whose request has ended.

A stand-in is never missing, because no reservation counts in a day once the next day is
open without it. A write can be sent before midnight and made after it, when another task has
already read the day and opened the next one. So when a write answers, the ledger reads the clock
again, and if the day has changed it reads the next day's item: the reservation counts if that
item holds its stand-in, or if it is not there yet and the write answered within `OPENING_SECONDS`
(60) of midnight, so that whoever opens the day can still find the lease. If not, one more write
takes the lease and its tokens back, as a settlement that charges nothing, and the request is
refused with `concurrency_limit`, which the client retries.

"Not there yet" is not enough on its own: whoever opens the day may have read the day before
already and not written yet. A day opened within those 60 seconds is therefore written
unconfirmed (the item's `opening` attribute). Its opener reads the day before again once its write
has answered, and if a live lease appeared there in between, one more write takes its own
reservation back, carries the stand-ins it missed and confirms the day, and that request too is
refused with `concurrency_limit`. Until the day is confirmed, no reservation takes a slot in it as
written: its write requires a confirmed day, and finding one unconfirmed it reads the day before
and sends a write that carries the stand-ins the day lacks, confirms the day, and adds its own
lease only if the limit still allows once they are counted. A day opened later needs none of this:
every lease of the day before that counts in it answered before its opener read that day. Every
request thus makes two writes at most, and a write that crosses midnight alone still makes one. So
the leases that count in a day were all written before the next day was opened, or were carried
into it, and from then on they can only go. That rests on the clocks of the gateway's tasks
agreeing on the time: a reservation that a task whose clock is behind confirms in the old day,
after another task has read that day, has no stand-in.

**The input estimate** is computed from the request body alone, before any call to the engine: a
token for every four bytes, rounded up. The design gives no rule. Four bytes a token is the usual
ratio for text; the estimate is the least a request that ends badly is charged, and the
settlement of a clean end replaces it by the engine's count.

**The lease.** A reservation is taken before the hold (section 6.2, steps 3 and 5), so a lease
outlasts the longest hold and then the longest request: `lease_seconds` is the pool's
`hold_seconds` plus the longest request plus 60 seconds, 2,280 seconds with the defaults of
section 17. From its expiry on, a lease holds no slot and is worth its input estimate, for every
reader and writer of the record:

- the read counts it so;
- a reservation that the record as written would refuse removes the expired leases, each charged
  its estimate, in its own single write, and is admitted if that makes room; an expired stand-in
  goes the same way, and gives back no tokens, having reserved none;
- a settlement that comes after the expiry removes the lease and charges the estimate.

An expired lease can thus stay in the item until one of these writes removes it, and it never
refuses a request nor lowers the tokens left by more than its estimate.

**Two ledgers.** `InMemoryLedger` keeps the days in memory, and `DynamoDBLedger` in the usage
table. Both decide with the same functions over a `UsageDay`. The in-memory one applies them under
a lock; the DynamoDB one restates them as the condition of its write and, when the condition
fails, decides from the item the failed write returns. Both are called from threads: the calls
block, and the caller keeps them off the event loop.

**The affinity cookie** (section 5.5). The same two writes carry the cookie the internal load
balancer set for the student and the request's pool, which keeps the student on one instance
(`rhi_gateway.affinity`). It is the record's `affinity_cookies`, a map from a pool's name to the
cookie, which holds no pool until a cookie is stored. The reservation's write returns the cookie
the record holds for the request's pool (`Reservation.cookie`), and the settlement's write stores
the one the request ended with, when it ended with one. So affinity costs no call of its own,
whatever else is in flight, and every gateway task sees the same cookie. No other write touches
the map: not the write that takes a reservation back, not the removal of expired leases, and a
settlement that finds nothing to settle stores nothing. A day's first reservation, which reads the
day before for its stand-ins, writes the new day with the cookies that day held, so that a student
keeps their instance across midnight. A request that started the day before settles into that
day's item, and its cookie stays there. A student's requests in flight at once each store theirs,
and for one pool the last settlement's stays.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Protocol

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from rhi_common.causes import Cause
from rhi_common.students import Lease, UsageDay, utc_day
from rhi_gateway.errors import GatewayError

BYTES_PER_TOKEN = 4
# What a lease lasts beyond the longest hold and the longest request.
LEASE_MARGIN_SECONDS = 60
# The retention of every store: the course end plus 90 days (section 6.6).
USAGE_RETENTION_SECONDS = 90 * 86_400
# How long after midnight a reservation of the day before may still count without a stand-in in
# the new day, when that day is not opened yet (see "Midnight" above).
OPENING_SECONDS = 60

# How many times the DynamoDB ledger sends a reservation whose record changed under it.
_ATTEMPTS = 10
# What the name of a stand-in begins with: the day of the request it stands for.
_STAND_IN = re.compile(r"\d{4}-\d{2}-\d{2}/", re.ASCII)


def estimate_input_tokens(body: bytes) -> int:
    """The estimate of a request's input, from its body alone: a token for every four bytes."""
    return -(-len(body) // BYTES_PER_TOKEN)


def lease_seconds(hold_seconds: int, longest_request_seconds: int) -> int:
    """How long a lease lasts: the pool's hold, then the longest request, then 60 seconds."""
    if hold_seconds < 0 or longest_request_seconds < 0:
        raise ValueError("a duration is negative")
    return hold_seconds + longest_request_seconds + LEASE_MARGIN_SECONDS


class Student(Protocol):
    """What the ledger reads of a student: the fields a key item and the gateway's lookup of it share.

    `rhi_common.students.StudentKey` and `rhi_gateway.keystore.KeyRecord` both have them.
    `expires_at` is the course end, which the retention of the usage item counts from.
    """

    @property
    def github_id(self) -> int: ...

    @property
    def alias(self) -> str: ...

    @property
    def expires_at(self) -> int: ...

    @property
    def daily_token_budget(self) -> int: ...

    @property
    def concurrency_limit(self) -> int: ...


@dataclass(frozen=True)
class Reservation:
    """What a request reserved: where its lease is, and the lease. The settlement takes it back.

    `pool` is the pool the request is for, if the reservation named one, and `cookie` the
    load balancer's cookie the record held for that pool when the reservation was written: `None`
    if it held none.
    """

    github_id: int
    day: str
    request_id: str
    lease: Lease
    pool: str | None = None
    cookie: str | None = None


class Ledger(Protocol):
    """A student's budget and concurrency slots: the two writes of a request, and one read."""

    def reserve(
        self,
        student: Student,
        request_id: str,
        body: bytes,
        *,
        output_limit: int,
        hold_seconds: int,
        longest_request_seconds: int,
        pool: str | None = None,
    ) -> Reservation:
        """Reserve the estimate of `body` plus `output_limit`, and a slot, or raise the refusal.

        `request_id` is the gateway's own ID for the request, `hold_seconds` the pool's, and
        `longest_request_seconds` the gateway's setting. An ID that is empty, that holds a
        reservation already, or that reads as a stand-in (a date, then `/`) is a `ValueError`.
        `pool` is the name of the pool the request is for: the reservation returns the load
        balancer's cookie the record holds for it. Without one, it returns none.
        """
        ...

    def settle(
        self,
        reservation: Reservation,
        *,
        input_tokens: int,
        output_tokens: int,
        ended_cleanly: bool,
        cookie: str | None = None,
    ) -> int | None:
        """Free the slot and charge the request: the tokens charged, or `None` if nothing was.

        `input_tokens` is the whole input, cached or not. After a clean end the two counts are the
        usage the engine reported; after any other end, the tokens observed. Nothing is charged
        when the reservation is settled already, by an earlier call or by a reservation that
        removed it after its expiry. `cookie` is the load balancer's cookie the request ended
        with: the same write stores it for the reservation's pool. `None`, for a request that
        ended with none, leaves the record's cookies as they are, and so does a settlement that
        charges nothing.
        """
        ...

    def tokens_left(self, student: Student) -> int:
        """The tokens of today's budget neither charged nor reserved. Writes nothing."""
        ...


# What both ledgers decide with: what a reservation, a settlement and a read do to a day.


def _day_before(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def _live(day: UsageDay | None, now: int) -> dict[str, Lease]:
    return {} if day is None else {request: lease for request, lease in day.leases.items() if lease.expires > now}


def _new_day(student: Student, day: str, before: UsageDay | None, now: int) -> UsageDay:
    """A day as its first reservation finds it: no tokens, and a stand-in for each live lease of `before`.

    It starts with the load balancer's cookies `before` held, so that midnight moves no student.
    """
    return UsageDay(
        github_id=student.github_id,
        day=day,
        alias=student.alias,
        tokens=0,
        expires_at=student.expires_at + USAGE_RETENTION_SECONDS,
        leases={
            f"{_day_before(day)}/{request}": Lease(tokens=0, estimate=0, expires=lease.expires)
            for request, lease in _live(before, now).items()
        },
        affinity_cookies={} if before is None else dict(before.affinity_cookies),
    )


def _new_lease(body: bytes, output_limit: int, expires: int) -> Lease:
    if output_limit < 0:
        raise ValueError("the output limit is negative")
    estimate = estimate_input_tokens(body)
    return Lease(tokens=estimate + output_limit, estimate=estimate, expires=expires)


def _expired(day: UsageDay, now: int) -> dict[str, Lease]:
    return {request: lease for request, lease in day.leases.items() if lease.expires <= now}


def _released(expired: Mapping[str, Lease]) -> int:
    """The tokens that settling these leases at their estimates gives back."""
    return sum(lease.tokens - lease.estimate for lease in expired.values())


def _without(day: UsageDay, expired: Mapping[str, Lease]) -> UsageDay:
    """The day once these leases are removed, each charged its estimate."""
    leases = {request: lease for request, lease in day.leases.items() if request not in expired}
    return replace(day, tokens=day.tokens - _released(expired), leases=leases)


def _refusal(day: UsageDay, student: Student, amount: int) -> Cause | None:
    """Why this day cannot take `amount` more tokens and one more lease, if it cannot."""
    if day.tokens + amount > student.daily_token_budget:
        return Cause.BUDGET_REACHED
    if len(day.leases) >= student.concurrency_limit:
        return Cause.CONCURRENCY_LIMIT
    return None


def _ended(day: UsageDay, before: Callable[[], UsageDay | None], now: int) -> dict[str, Lease]:
    """The stand-ins of this day, not yet expired, whose request holds no live lease in the day before.

    `before` reads the item of the day before, and is called only if the day holds such a stand-in.
    """
    prefix = _day_before(day.day) + "/"
    stand_ins = {name: lease for name, lease in _live(day, now).items() if name.startswith(prefix)}
    if not stand_ins:
        return {}
    live = _live(before(), now)
    return {name: lease for name, lease in stand_ins.items() if name.removeprefix(prefix) not in live}


def _to_remove(
    day: UsageDay,
    student: Student,
    request_id: str,
    lease: Lease,
    now: int,
    before: Callable[[], UsageDay | None],
) -> dict[str, Lease]:
    """The leases a reservation removes to be admitted to this day; raises the refusal.

    None when the day as written admits it. The expired ones when only that makes room, and with
    them the stand-ins of requests that have ended when only that gives a slot.
    """
    if not request_id or _STAND_IN.match(request_id):
        raise ValueError("a reservation needs the request's ID, and one that is not a stand-in's")
    if request_id in day.leases:
        raise ValueError("this request holds a reservation already")
    if _refusal(day, student, lease.tokens) is None:
        return {}
    gone = _expired(day, now)
    cause = _refusal(_without(day, gone), student, lease.tokens)
    if cause is Cause.CONCURRENCY_LIMIT:
        gone |= _ended(day, before, now)
        cause = _refusal(_without(day, gone), student, lease.tokens)
    if cause is not None:
        raise GatewayError(cause, "the student's day does not admit this request")
    return gone


def _reserved(day: UsageDay, request_id: str, lease: Lease, expired: Mapping[str, Lease]) -> UsageDay:
    swept = _without(day, expired)
    return replace(swept, tokens=swept.tokens + lease.tokens, leases={**swept.leases, request_id: lease})


def _reserve_within_the_day(
    clock: Callable[[], float],
    student: Student,
    request_id: str,
    body: bytes,
    output_limit: int,
    seconds: int,
    pool: str | None,
    write: Callable[[Reservation, int], UsageDay],
    read: Callable[[int, str], UsageDay | None],
    take_back: Callable[[Reservation], object],
) -> Reservation:
    """A reservation that `write` made, and that counts in the day it was confirmed in.

    `write` adds the reservation to the record of its day and returns the record as it wrote it,
    or raises the refusal; `read` reads a student's day without writing. The reservation returned
    holds the cookie that record has for `pool`. The clock is read again when `write` returns. If
    the day has changed and the next day was opened without this lease, `take_back` removes the
    lease and its tokens, and the request is refused.
    """
    now = int(clock())
    lease = _new_lease(body, output_limit, now + seconds)
    reservation = Reservation(student.github_id, utc_day(now), request_id, lease, pool)
    held = write(reservation, now).affinity_cookies
    reservation = replace(reservation, cookie=None if pool is None else held.get(pool))
    answered = int(clock())
    if utc_day(answered) == reservation.day or _carried(reservation, answered, read):
        return reservation
    take_back(reservation)
    raise GatewayError(Cause.CONCURRENCY_LIMIT, "the next day was opened while this reservation was made")


def _midnight(day: str) -> int:
    """When a UTC day begins, in seconds since the epoch."""
    return (date.fromisoformat(day) - date(1970, 1, 1)).days * 86_400


def _carried(reservation: Reservation, answered: int, read: Callable[[int, str], UsageDay | None]) -> bool:
    """Whether a reservation whose write answered at `answered`, after its day, counts in the next day.

    It does if that day holds its stand-in, or if the day is not opened yet and the write answered
    within `OPENING_SECONDS` of midnight.
    """
    today = utc_day(answered)
    if _day_before(today) != reservation.day:
        return False
    found = read(reservation.github_id, today)
    if found is None:
        return answered < _midnight(today) + OPENING_SECONDS
    return f"{reservation.day}/{reservation.request_id}" in found.leases


def _charge(lease: Lease, input_tokens: int, output_tokens: int, ended_cleanly: bool, now: int) -> int:
    """What a request is charged when it is settled at `now`."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("a count of tokens is negative")
    if lease.expires <= now:
        return lease.estimate
    used = input_tokens + output_tokens
    return used if ended_cleanly else max(used, lease.estimate)


def _left(day: UsageDay | None, student: Student, now: int) -> int:
    if day is None:
        return max(0, student.daily_token_budget)
    return max(0, student.daily_token_budget - _without(day, _expired(day, now)).tokens)


class InMemoryLedger:
    """The ledger in memory: for tests and for a gateway run without a table.

    `writes` counts the writes to the students' records. `day` returns a record as it is written.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._days: dict[tuple[int, str], UsageDay] = {}
        self.writes = 0

    def day(self, github_id: int, day: str) -> UsageDay | None:
        with self._lock:
            return self._days.get((github_id, day))

    def reserve(
        self,
        student: Student,
        request_id: str,
        body: bytes,
        *,
        output_limit: int,
        hold_seconds: int,
        longest_request_seconds: int,
        pool: str | None = None,
    ) -> Reservation:
        def write(reservation: Reservation, now: int) -> UsageDay:
            def before() -> UsageDay | None:
                return self._days.get((reservation.github_id, _day_before(reservation.day)))

            with self._lock:
                day = self._days.get((reservation.github_id, reservation.day))
                if day is None:
                    day = _new_day(student, reservation.day, before(), now)
                gone = _to_remove(day, student, request_id, reservation.lease, now, before)
                written = _reserved(day, request_id, reservation.lease, gone)
                self._write(written)
            return written

        return _reserve_within_the_day(
            self._clock,
            student,
            request_id,
            body,
            output_limit,
            lease_seconds(hold_seconds, longest_request_seconds),
            pool,
            write,
            self.day,
            lambda reservation: self._remove(reservation, 0),
        )

    def settle(
        self,
        reservation: Reservation,
        *,
        input_tokens: int,
        output_tokens: int,
        ended_cleanly: bool,
        cookie: str | None = None,
    ) -> int | None:
        charge = _charge(reservation.lease, input_tokens, output_tokens, ended_cleanly, int(self._clock()))
        return charge if self._remove(reservation, charge, cookie=cookie) else None

    def _remove(self, reservation: Reservation, charge: int, *, cookie: str | None = None) -> bool:
        """Remove the lease and replace its tokens by `charge`, if the lease is still there.

        The same write stores `cookie`, if there is one, for the reservation's pool.
        """
        with self._lock:
            day = self._days.get((reservation.github_id, reservation.day))
            if day is None or reservation.request_id not in day.leases:
                return False
            leases = {request: held for request, held in day.leases.items() if request != reservation.request_id}
            cookies = day.affinity_cookies
            if cookie is not None and reservation.pool is not None:
                cookies = {**cookies, reservation.pool: cookie}
            self._write(
                replace(
                    day, tokens=day.tokens + charge - reservation.lease.tokens, leases=leases, affinity_cookies=cookies
                )
            )
        return True

    def tokens_left(self, student: Student) -> int:
        now = int(self._clock())
        return _left(self.day(student.github_id, utc_day(now)), student, now)

    def _write(self, day: UsageDay) -> None:
        self._days[(day.github_id, day.day)] = day
        self.writes += 1


# The attributes of the usage item, each under a placeholder: `day` is a reserved word of
# DynamoDB's expressions, and a placeholder for every name needs no list of the others.
_NAMES = {"#tokens": "tokens", "#leases": "leases"}
# The attribute that holds the load balancer's cookies, by pool. A reservation that names a pool
# writes it empty where there is none, so that the settlement has a map to store its cookie in.
_COOKIES = "affinity_cookies"
# The attribute of a day whose opener has not confirmed it yet (see the module's "Midnight").
_OPENING = "opening"
_CONDITION_FAILED = "ConditionalCheckFailedException"
_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _values(values: Mapping[str, object]) -> dict[str, Any]:
    """Values in DynamoDB's own form, as its client takes them."""
    return {name: _serializer.serialize(value) for name, value in values.items()}


def _missing_stand_ins(day: UsageDay, before: UsageDay | None, now: int) -> dict[str, Lease]:
    """The stand-ins this day lacks: one for each live lease of the day before that has none here."""
    prefix = _day_before(day.day) + "/"
    return {
        prefix + request: Lease(tokens=0, estimate=0, expires=lease.expires)
        for request, lease in _live(before, now).items()
        if prefix + request not in day.leases
    }


def _stand_in_updates(stand_ins: Mapping[str, Lease], values: dict[str, object]) -> tuple[dict[str, str], list[str]]:
    """The names and the `SET` actions that add these stand-ins to an item; their values go in `values`."""
    names = {f"#carried{number}": name for number, name in enumerate(stand_ins)}
    for number, lease in enumerate(stand_ins.values()):
        values[f":carried{number}"] = Lease(tokens=0, estimate=0, expires=lease.expires).to_map()
    return names, [f"#leases.{name} = :{name[1:]}" for name in names]


class DynamoDBLedger:
    """The ledger in the usage table (`rhi_common.tables.USAGE`).

    `client` is a DynamoDB client of the AWS SDK, which threads may share, and `table_name` the
    deployed table's name. A reservation is one call, and more only when that call writes
    nothing: for the student's first request of a day, whose item is not there yet and is written
    after a read of the day before (and, within a minute of midnight, followed by a second read and
    taken back in one more call if the day before gained a live lease in between), for the first
    request after such a day, which reads the day before and confirms it, and for a request that
    expired leases or stand-ins are in the way of (for stand-ins, with one read of the day
    before). It gives up, with
    `concurrency_limit`, after ten calls that each found the record changed by another request. A
    reservation whose write answers after its day has ended reads the next day's item, and is
    taken back, in one call, only if that day was opened without it. A settlement is one call,
    and the read one consistent read.

    The reservation's write returns the item as it wrote it, which holds the student's cookies, so
    the cookie costs no call. The settlement's write stores the request's cookie under its pool's
    name in the map of cookies. DynamoDB refuses a write into a map that is not there, and one
    write cannot both make the map and add to it. So the write of a reservation that names a pool
    leaves an empty map where the item had none, which reads as no cookie: the map is there for
    every settlement that stores one, and each is one call whatever other requests of the student
    are in flight.

    An error of DynamoDB other than a failed condition is raised as the SDK raised it. It carries
    no cause, so the gateway renders it as `engine_error`, and none of its text leaves the gateway.
    """

    def __init__(self, client: Any, table_name: str, *, clock: Callable[[], float] = time.time) -> None:
        self._client = client
        self._table = table_name
        self._clock = clock

    def reserve(
        self,
        student: Student,
        request_id: str,
        body: bytes,
        *,
        output_limit: int,
        hold_seconds: int,
        longest_request_seconds: int,
        pool: str | None = None,
    ) -> Reservation:
        return _reserve_within_the_day(
            self._clock,
            student,
            request_id,
            body,
            output_limit,
            lease_seconds(hold_seconds, longest_request_seconds),
            pool,
            lambda reservation, now: self._write_reservation(student, reservation, now),
            self._read,
            lambda reservation: self._remove(reservation, 0),
        )

    def settle(
        self,
        reservation: Reservation,
        *,
        input_tokens: int,
        output_tokens: int,
        ended_cleanly: bool,
        cookie: str | None = None,
    ) -> int | None:
        charge = _charge(reservation.lease, input_tokens, output_tokens, ended_cleanly, int(self._clock()))
        return charge if self._remove(reservation, charge, cookie=cookie) else None

    def tokens_left(self, student: Student) -> int:
        now = int(self._clock())
        return _left(self._read(student.github_id, utc_day(now)), student, now)

    def _write_reservation(self, student: Student, reservation: Reservation, now: int) -> UsageDay:
        """Add the reservation to the item of its day and return the day as written, or raise the refusal."""
        request_id, lease = reservation.request_id, reservation.lease

        def before() -> UsageDay | None:
            return self._read(reservation.github_id, _day_before(reservation.day))

        # What no day admits is refused before any call.
        _to_remove(_new_day(student, reservation.day, None, now), student, request_id, lease, now, before)
        gone: dict[str, Lease] = {}
        carried: dict[str, Lease] | None = None
        for _ in range(_ATTEMPTS):
            written, item = self._add_lease(student, reservation, gone, carried)
            if written is not None:
                return written
            if item is None:
                written, item = self._open_day(student, reservation, now)
                if written is not None:
                    return written
            # The write changed nothing. Decide from the day it found: refuse, or send it again
            # without the leases that have expired and the stand-ins of requests that have ended,
            # and, in a day not yet confirmed, with the stand-ins its opener did not write.
            gone, carried = {}, None
            if item is None:
                continue
            found = _day(item)
            if _OPENING in item:
                yesterday = before()
                carried = _missing_stand_ins(found, yesterday, now)
                found = replace(found, leases={**found.leases, **carried})
                gone = _to_remove(found, student, request_id, lease, now, lambda read=yesterday: read)
            else:
                gone = _to_remove(found, student, request_id, lease, now, before)
        # Every attempt found a record that another request had just changed.
        raise GatewayError(Cause.CONCURRENCY_LIMIT, "the student's day kept changing under this request")

    def _remove(
        self,
        reservation: Reservation,
        charge: int,
        *,
        stand_ins: Mapping[str, Lease] | None = None,
        cookie: str | None = None,
    ) -> bool:
        """Remove the lease and replace its tokens by `charge`, in one write, if the lease is still there.

        `stand_ins`, given only by the opener of a day that takes its reservation back, are the
        stand-ins it missed; the write carries them into the day and confirms the day. `cookie`,
        given only by a settlement, is stored for the reservation's pool in the same write.
        """
        values: dict[str, object] = {":change": charge - reservation.lease.tokens}
        names, sets = _stand_in_updates(stand_ins or {}, values)
        update = "REMOVE #leases.#request" + (", #opening" if stand_ins is not None else "") + " ADD #tokens :change"
        condition = "attribute_exists(#leases.#request)"
        if stand_ins is not None:
            names["#opening"] = _OPENING
        if cookie is not None and reservation.pool is not None:
            # The map is there: this request's reservation named the pool, and so left one.
            names |= {"#cookies": _COOKIES, "#pool": reservation.pool}
            sets.append("#cookies.#pool = :cookie")
            values[":cookie"] = cookie
        if sets:
            update += " SET " + ", ".join(sets)
        written, _ = self._conditional(
            self._client.update_item,
            Key=self._key(reservation.github_id, reservation.day),
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames={**_NAMES, "#request": reservation.request_id, **names},
            ExpressionAttributeValues=_values(values),
        )
        return written

    def _read(self, github_id: int, day: str) -> UsageDay | None:
        """A student's day as it is written, in one consistent read."""
        answer = self._client.get_item(TableName=self._table, Key=self._key(github_id, day), ConsistentRead=True)
        item = answer.get("Item")
        return None if not item else _day(item)

    def _add_lease(
        self,
        student: Student,
        reservation: Reservation,
        gone: Mapping[str, Lease],
        carried: Mapping[str, Lease] | None,
    ) -> tuple[UsageDay | None, Mapping[str, Any] | None]:
        """The reservation as one write to an item that is there: `_refusal` and `_reserved`, restated.

        Removing `gone` gives back their tokens and their slots, so the condition allows for
        both, and holds only while each of them is still in the item. The item must be a
        confirmed day, unless `carried` is given: then the write confirms the day itself, adding
        those stand-ins, which take their slots, and holds only while the day is still unconfirmed.
        For a reservation that names a pool, it leaves an empty map of cookies where there is none.
        Returns the day as this write left it, which the write itself returns, or `None` and the
        item it found when it wrote nothing.
        """
        lease = reservation.lease
        values: dict[str, object] = {
            ":lease": lease.to_map(),
            ":change": lease.tokens - _released(gone),
            ":room": student.daily_token_budget - lease.tokens + _released(gone),
            ":slots": student.concurrency_limit + len(gone) - len(carried or {}),
        }
        names, sets = _stand_in_updates(carried or {}, values)
        if reservation.pool is not None:
            names["#cookies"] = _COOKIES
            sets.append("#cookies = if_not_exists(#cookies, :none)")
            values[":none"] = {}
        removed = {f"#gone{number}": request for number, request in enumerate(gone)}
        update = "SET " + ", ".join(["#leases.#request = :lease", *sets]) + " ADD #tokens :change"
        condition = "#tokens <= :room AND size(#leases) < :slots AND attribute_not_exists(#leases.#request)"
        condition += " AND attribute_not_exists(#opening)" if carried is None else " AND attribute_exists(#opening)"
        removals = [f"#leases.{name}" for name in removed] + ([] if carried is None else ["#opening"])
        if removals:
            update += " REMOVE " + ", ".join(removals)
        condition += "".join(f" AND attribute_exists(#leases.{name})" for name in removed)
        written, item = self._conditional(
            self._client.update_item,
            Key=self._key(reservation.github_id, reservation.day),
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames={
                **_NAMES,
                "#opening": _OPENING,
                "#request": reservation.request_id,
                **removed,
                **names,
            },
            ExpressionAttributeValues=_values(values),
            ReturnValues="ALL_NEW",
        )
        return (_day(item), None) if written and item is not None else (None, item)

    def _open_day(
        self, student: Student, reservation: Reservation, now: int
    ) -> tuple[UsageDay | None, Mapping[str, Any] | None]:
        """The student's first reservation of a day: the item written whole, only if the day has none.

        Returns the day it wrote, or `None` and the item that was there already. It holds a
        stand-in for each request still running from the day before, read here, and the cookies
        that day held. The refusal is raised if the stand-ins leave no slot. Opened within
        `OPENING_SECONDS` of midnight,
        it is written unconfirmed (`opening`): until a write confirms it, no reservation takes a
        slot in it without first reading the day before again and carrying the stand-ins missing
        from it. Opened later, the read above already holds every lease of the day before that
        counts in this day.
        """
        yesterday = _day_before(reservation.day)
        before = self._read(reservation.github_id, yesterday)
        fresh = _new_day(student, reservation.day, before, now)
        _to_remove(fresh, student, reservation.request_id, reservation.lease, now, lambda: before)
        day = _reserved(fresh, reservation.request_id, reservation.lease, {})
        opening = now < _midnight(reservation.day) + OPENING_SECONDS
        item = day.to_item()
        if reservation.pool is not None:
            # The map of cookies, empty if the day before held none: see `_add_lease`.
            item[_COOKIES] = dict(day.affinity_cookies)
        written, found = self._conditional(
            self._client.put_item,
            Item=_values({**item, _OPENING: True} if opening else item),
            ConditionExpression="attribute_not_exists(#day)",
            ExpressionAttributeNames={"#day": "day"},
        )
        if not written:
            return None, found
        if not opening:
            return day, None
        # A reservation of the day before may have been written after the read above, and have
        # found no item of this day when it checked: it counts, and its stand-in is not here.
        late = _missing_stand_ins(fresh, self._read(reservation.github_id, yesterday), now)
        if late:
            self._remove(reservation, 0, stand_ins=late)
            raise GatewayError(Cause.CONCURRENCY_LIMIT, "the day before changed while this day was opened")
        return day, None

    def _conditional(self, write: Callable[..., Any], **arguments: Any) -> tuple[bool, Mapping[str, Any] | None]:
        """Make one conditional write: whether it was written, and an item.

        The item is the one found there if nothing was written, and else the one the write
        returns, when `arguments` ask it for one (`ReturnValues`).
        """
        try:
            answer = write(TableName=self._table, ReturnValuesOnConditionCheckFailure="ALL_OLD", **arguments)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != _CONDITION_FAILED:
                raise
            item = error.response.get("Item")
            return False, item or None
        return True, answer.get("Attributes") or None

    @staticmethod
    def _key(github_id: int, day: str) -> dict[str, Any]:
        return _values({"github_id": github_id, "day": day})


def _day(item: Mapping[str, Any]) -> UsageDay:
    """A usage item as the client returns it, read as its record."""
    return UsageDay.from_item({name: _deserializer.deserialize(value) for name, value in item.items()})
