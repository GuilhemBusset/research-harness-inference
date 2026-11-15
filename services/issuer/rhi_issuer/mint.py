"""Minting a key for a student of the roster (system design, sections 6.2 and 6.4).

`mint` makes a key in the format of `rhi_common.keys` and writes its item, a
`rhi_common.students.StudentKey`, under the key's SHA-256: the key itself is in what `mint`
returns and nowhere else, neither in a table nor in a log, and this module logs nothing. The item
carries the alias, the daily token budget and the concurrency limit of the student's roster entry
as they are when the key is minted, so that the gateway answers a request with one read of the
keys table and never reads the roster.

A key expires at the course end. The course end is a date, and the key works through that day:
its `expires_at` is the first second of the next day, in UTC. The function is given the date and
no time zone.

Each login shows when the student's previous key was issued, so that a student notices a login
they did not make: `previous_issued_at` is the time of issue of the student's newest key before
this one, read from the keys table's index by student, or `None` when there is none.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from rhi_common.keys import generate_key, key_hash
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import BY_STUDENT, KEYS


@dataclass(frozen=True)
class Minted:
    """A new key, in clear, and what the login shows with it. Times are whole seconds since the epoch."""

    key: str
    issued_at: int
    expires_at: int
    previous_issued_at: int | None


def expiry(course_end: datetime.date) -> int:
    """When a key of a course that ends on that date stops working: the end of that day, in UTC."""
    day_after = course_end + datetime.timedelta(days=1)
    return int(datetime.datetime.combine(day_after, datetime.time.min, datetime.UTC).timestamp())


def _previous_issue(github_id: int, keys: Any) -> int | None:
    """When the student's newest key was issued; `None` when the student has no key."""
    index = KEYS.index(BY_STUDENT)
    answer = keys.query(
        IndexName=index.name,
        KeyConditionExpression="#student = :student",
        ExpressionAttributeNames={"#student": index.partition_key.name},
        ExpressionAttributeValues={":student": github_id},
        # The index is sorted by the time of issue: the newest key first, and only that one.
        ScanIndexForward=False,
        Limit=1,
    )
    items = answer.get("Items") or []
    return int(items[0]["issued_at"]) if items else None


def mint(entry: RosterEntry, installation: str, *, course_end: datetime.date, now: int, keys: Any) -> Minted:
    """A new key for the student of that roster entry, stored as its SHA-256.

    `installation` is the identifier the CLI sent with the device code, `now` the time of issue in
    whole seconds since the epoch, and `keys` the keys table, as the table resource of the AWS SDK.
    """
    previous = _previous_issue(entry.github_id, keys)
    key = generate_key()
    record = StudentKey(
        key_hash=key_hash(key),
        github_id=entry.github_id,
        alias=entry.alias,
        installation=installation,
        issued_at=now,
        expires_at=expiry(course_end),
        revoked_at=None,
        daily_token_budget=entry.daily_token_budget,
        concurrency_limit=entry.concurrency_limit,
    )
    # Never over an item that is there: two keys do not share a SHA-256.
    keys.put_item(
        Item=record.to_item(),
        ConditionExpression="attribute_not_exists(#hash)",
        ExpressionAttributeNames={"#hash": KEYS.partition_key.name},
    )
    return Minted(key=key, issued_at=record.issued_at, expires_at=record.expires_at, previous_issued_at=previous)
