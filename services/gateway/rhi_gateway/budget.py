"""Reserve budget before a request, settle after it (system design, section 6.2, step 3).

Checking a budget only at admission, or counting tokens only at the end, lets a client overspend
by opening many streams or by hanging up early. So a request writes its student's record twice,
and this module is the one place where it does:

1. **The reservation**, before anything is sent to the engine. One conditional write adds the
   estimate of the input plus the request's output limit to the tokens of the student's day, and
   adds a lease under the request's ID. Its condition is the budget and the limit themselves: the
   day's tokens and this amount fit the daily budget, and the leases are fewer than the
   concurrency limit. Both numbers are the ones the key's lookup gave (`StudentKey`). Requests
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
after midnight. The limit is therefore counted per day, and a request still running at midnight
holds no slot of the new day. The item expires 90 days after the key's course end, the retention
of every store (section 6.6).

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
  its estimate, in its own single write, and is admitted if that makes room;
- a settlement that comes after the expiry removes the lease and charges the estimate.

An expired lease can thus stay in the item until one of these writes removes it, and it never
refuses a request nor lowers the tokens left by more than its estimate.

**Two ledgers.** `InMemoryLedger` keeps the days in memory, and `DynamoDBLedger` in the usage
table. Both decide with the same functions over a `UsageDay`. The in-memory one applies them under
a lock; the DynamoDB one restates them as the condition of its write and, when the condition
fails, decides from the item the failed write returns. Both are called from threads: the calls
block, and the caller keeps them off the event loop.

Phase 2 makes the same two writes carry the student's affinity cookie for the pool (section 5.5).
That is not built here.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from rhi_common.causes import Cause
from rhi_common.students import Lease, StudentKey, UsageDay, utc_day
from rhi_gateway.errors import GatewayError

BYTES_PER_TOKEN = 4
# What a lease lasts beyond the longest hold and the longest request.
LEASE_MARGIN_SECONDS = 60
# The retention of every store: the course end plus 90 days (section 6.6).
USAGE_RETENTION_SECONDS = 90 * 86_400

# How many times the DynamoDB ledger sends a reservation whose record changed under it.
_ATTEMPTS = 10


def estimate_input_tokens(body: bytes) -> int:
    """The estimate of a request's input, from its body alone: a token for every four bytes."""
    return -(-len(body) // BYTES_PER_TOKEN)


def lease_seconds(hold_seconds: int, longest_request_seconds: int) -> int:
    """How long a lease lasts: the pool's hold, then the longest request, then 60 seconds."""
    if hold_seconds < 0 or longest_request_seconds < 0:
        raise ValueError("a duration is negative")
    return hold_seconds + longest_request_seconds + LEASE_MARGIN_SECONDS


@dataclass(frozen=True)
class Reservation:
    """What a request reserved: where its lease is, and the lease. The settlement takes it back."""

    github_id: int
    day: str
    request_id: str
    lease: Lease


class Ledger(Protocol):
    """A student's budget and concurrency slots: the two writes of a request, and one read."""

    def reserve(
        self,
        student: StudentKey,
        request_id: str,
        body: bytes,
        *,
        output_limit: int,
        hold_seconds: int,
        longest_request_seconds: int,
    ) -> Reservation:
        """Reserve the estimate of `body` plus `output_limit`, and a slot, or raise the refusal.

        `request_id` is the gateway's own ID for the request, `hold_seconds` the pool's, and
        `longest_request_seconds` the gateway's setting.
        """
        ...

    def settle(
        self, reservation: Reservation, *, input_tokens: int, output_tokens: int, ended_cleanly: bool
    ) -> int | None:
        """Free the slot and charge the request: the tokens charged, or `None` if nothing was.

        `input_tokens` is the whole input, cached or not. After a clean end the two counts are the
        usage the engine reported; after any other end, the tokens observed. Nothing is charged
        when the reservation is settled already, by an earlier call or by a reservation that
        removed it after its expiry.
        """
        ...

    def tokens_left(self, student: StudentKey) -> int:
        """The tokens of today's budget neither charged nor reserved. Writes nothing."""
        ...


# What both ledgers decide with: what a reservation, a settlement and a read do to a day.


def _new_day(student: StudentKey, day: str) -> UsageDay:
    return UsageDay(
        github_id=student.github_id,
        day=day,
        alias=student.alias,
        tokens=0,
        expires_at=student.expires_at + USAGE_RETENTION_SECONDS,
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


def _refusal(day: UsageDay, student: StudentKey, amount: int) -> Cause | None:
    """Why this day cannot take `amount` more tokens and one more lease, if it cannot."""
    if day.tokens + amount > student.daily_token_budget:
        return Cause.BUDGET_REACHED
    if len(day.leases) >= student.concurrency_limit:
        return Cause.CONCURRENCY_LIMIT
    return None


def _to_remove(day: UsageDay, student: StudentKey, request_id: str, lease: Lease, now: int) -> dict[str, Lease]:
    """The expired leases a reservation removes to be admitted to this day; raises the refusal.

    None when the day as written admits it. All of them when only that makes room.
    """
    if not request_id:
        raise ValueError("a reservation needs the request's ID")
    if request_id in day.leases:
        raise ValueError("this request holds a reservation already")
    if _refusal(day, student, lease.tokens) is None:
        return {}
    expired = _expired(day, now)
    cause = _refusal(_without(day, expired), student, lease.tokens)
    if cause is not None:
        raise GatewayError(cause, "the student's day does not admit this request")
    return expired


def _reserved(day: UsageDay, request_id: str, lease: Lease, expired: Mapping[str, Lease]) -> UsageDay:
    swept = _without(day, expired)
    return replace(swept, tokens=swept.tokens + lease.tokens, leases={**swept.leases, request_id: lease})


def _charge(lease: Lease, input_tokens: int, output_tokens: int, ended_cleanly: bool, now: int) -> int:
    """What a request is charged when it is settled at `now`."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("a count of tokens is negative")
    if lease.expires <= now:
        return lease.estimate
    used = input_tokens + output_tokens
    return used if ended_cleanly else max(used, lease.estimate)


def _left(day: UsageDay | None, student: StudentKey, now: int) -> int:
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
        student: StudentKey,
        request_id: str,
        body: bytes,
        *,
        output_limit: int,
        hold_seconds: int,
        longest_request_seconds: int,
    ) -> Reservation:
        now = int(self._clock())
        lease = _new_lease(body, output_limit, now + lease_seconds(hold_seconds, longest_request_seconds))
        reservation = Reservation(student.github_id, utc_day(now), request_id, lease)
        with self._lock:
            day = self._days.get((reservation.github_id, reservation.day)) or _new_day(student, reservation.day)
            expired = _to_remove(day, student, request_id, lease, now)
            self._write(_reserved(day, request_id, lease, expired))
        return reservation

    def settle(
        self, reservation: Reservation, *, input_tokens: int, output_tokens: int, ended_cleanly: bool
    ) -> int | None:
        lease = reservation.lease
        charge = _charge(lease, input_tokens, output_tokens, ended_cleanly, int(self._clock()))
        with self._lock:
            day = self._days.get((reservation.github_id, reservation.day))
            if day is None or reservation.request_id not in day.leases:
                return None
            leases = {request: held for request, held in day.leases.items() if request != reservation.request_id}
            self._write(replace(day, tokens=day.tokens + charge - lease.tokens, leases=leases))
        return charge

    def tokens_left(self, student: StudentKey) -> int:
        now = int(self._clock())
        return _left(self.day(student.github_id, utc_day(now)), student, now)

    def _write(self, day: UsageDay) -> None:
        self._days[(day.github_id, day.day)] = day
        self.writes += 1


# The attributes of the usage item, each under a placeholder: `day` is a reserved word of
# DynamoDB's expressions, and a placeholder for every name needs no list of the others.
_NAMES = {"#tokens": "tokens", "#leases": "leases"}
_CONDITION_FAILED = "ConditionalCheckFailedException"
_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _values(values: Mapping[str, object]) -> dict[str, Any]:
    """Values in DynamoDB's own form, as its client takes them."""
    return {name: _serializer.serialize(value) for name, value in values.items()}


class DynamoDBLedger:
    """The ledger in the usage table (`rhi_common.tables.USAGE`).

    `client` is a DynamoDB client of the AWS SDK, which threads may share, and `table_name` the
    deployed table's name. A reservation is one call, and more only when that call writes
    nothing: for the student's first request of a day, whose item is not there yet, and for a
    request that expired leases stand in the way of. It gives up, with `concurrency_limit`, after
    ten calls that each found the record changed by another request. A settlement is one call,
    and the read one consistent read.

    An error of DynamoDB other than a failed condition is raised as the SDK raised it. It carries
    no cause, so the gateway renders it as `engine_error`, and none of its text leaves the gateway.
    """

    def __init__(self, client: Any, table_name: str, *, clock: Callable[[], float] = time.time) -> None:
        self._client = client
        self._table = table_name
        self._clock = clock

    def reserve(
        self,
        student: StudentKey,
        request_id: str,
        body: bytes,
        *,
        output_limit: int,
        hold_seconds: int,
        longest_request_seconds: int,
    ) -> Reservation:
        now = int(self._clock())
        lease = _new_lease(body, output_limit, now + lease_seconds(hold_seconds, longest_request_seconds))
        reservation = Reservation(student.github_id, utc_day(now), request_id, lease)
        _to_remove(_new_day(student, reservation.day), student, request_id, lease, now)
        expired: dict[str, Lease] = {}
        for _ in range(_ATTEMPTS):
            written, found = self._add_lease(student, reservation, expired)
            if written:
                return reservation
            if found is None:
                # The student's first request of the day: the item is written whole.
                written, found = self._create(_reserved(_new_day(student, reservation.day), request_id, lease, {}))
                if written:
                    return reservation
            # The write changed nothing. Decide from the day it found: refuse, or send it again
            # without the leases that have expired.
            expired = {} if found is None else _to_remove(found, student, request_id, lease, now)
        # Every attempt found a record that another request had just changed.
        raise GatewayError(Cause.CONCURRENCY_LIMIT, "the student's day kept changing under this request")

    def settle(
        self, reservation: Reservation, *, input_tokens: int, output_tokens: int, ended_cleanly: bool
    ) -> int | None:
        lease = reservation.lease
        charge = _charge(lease, input_tokens, output_tokens, ended_cleanly, int(self._clock()))
        written, _ = self._conditional(
            self._client.update_item,
            Key=self._key(reservation.github_id, reservation.day),
            UpdateExpression="REMOVE #leases.#request ADD #tokens :change",
            ConditionExpression="attribute_exists(#leases.#request)",
            ExpressionAttributeNames={**_NAMES, "#request": reservation.request_id},
            ExpressionAttributeValues=_values({":change": charge - lease.tokens}),
        )
        return charge if written else None

    def tokens_left(self, student: StudentKey) -> int:
        now = int(self._clock())
        answer = self._client.get_item(
            TableName=self._table, Key=self._key(student.github_id, utc_day(now)), ConsistentRead=True
        )
        item = answer.get("Item")
        return _left(None if not item else _day(item), student, now)

    def _add_lease(
        self, student: StudentKey, reservation: Reservation, expired: Mapping[str, Lease]
    ) -> tuple[bool, UsageDay | None]:
        """The reservation as one write to an item that is there: `_refusal` and `_reserved`, restated.

        Removing `expired` gives back their tokens and their slots, so the condition allows for
        both, and holds only while each of them is still in the item.
        """
        lease = reservation.lease
        removed = {f"#expired{number}": request for number, request in enumerate(expired)}
        update = "SET #leases.#request = :lease ADD #tokens :change"
        condition = "#tokens <= :room AND size(#leases) < :slots AND attribute_not_exists(#leases.#request)"
        if removed:
            update += " REMOVE " + ", ".join(f"#leases.{name}" for name in removed)
            condition += "".join(f" AND attribute_exists(#leases.{name})" for name in removed)
        return self._conditional(
            self._client.update_item,
            Key=self._key(reservation.github_id, reservation.day),
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames={**_NAMES, "#request": reservation.request_id, **removed},
            ExpressionAttributeValues=_values(
                {
                    ":lease": lease.to_map(),
                    ":change": lease.tokens - _released(expired),
                    ":room": student.daily_token_budget - lease.tokens + _released(expired),
                    ":slots": student.concurrency_limit + len(expired),
                }
            ),
        )

    def _create(self, day: UsageDay) -> tuple[bool, UsageDay | None]:
        """Write a day's item, only if the day has none."""
        return self._conditional(
            self._client.put_item,
            Item=_values(day.to_item()),
            ConditionExpression="attribute_not_exists(#day)",
            ExpressionAttributeNames={"#day": "day"},
        )

    def _conditional(self, write: Callable[..., Any], **arguments: Any) -> tuple[bool, UsageDay | None]:
        """Make one conditional write: whether it was written and, if not, the day it found there."""
        try:
            write(TableName=self._table, ReturnValuesOnConditionCheckFailure="ALL_OLD", **arguments)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != _CONDITION_FAILED:
                raise
            item = error.response.get("Item")
            return False, None if not item else _day(item)
        return True, None

    @staticmethod
    def _key(github_id: int, day: str) -> dict[str, Any]:
        return _values({"github_id": github_id, "day": day})


def _day(item: Mapping[str, Any]) -> UsageDay:
    """A usage item as the client returns it, read as its record."""
    return UsageDay.from_item({name: _deserializer.deserialize(value) for name, value in item.items()})
