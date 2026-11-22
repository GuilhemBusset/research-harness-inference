"""Minting a key for a student of the roster (system design, sections 6.2 and 6.4).

`mint` makes a key in the format of `rhi_common.keys` and the item it is stored as, a
`rhi_common.students.StudentKey`, under the key's SHA-256: the key itself is in what `mint`
returns and nowhere else, neither in a table nor in a log, and this module logs nothing. The item
carries the alias and the concurrency limit of the student's roster entry as they are when the
key is minted, so that the gateway answers a request with one read of the
keys table and never reads the roster. `mint` writes nothing: `rhi_issuer.rules` decides whether
the key may be issued and stores it.

A key expires at the course end. The course end is a date, and the key works through that day:
its `expires_at` is the first second of the next day, in UTC. The function is given the date and
no time zone. A key item is personal data, and its table deletes it at its `expires_at`: that is
never later than the course end plus 90 days, when every personal row expires
(`rhi_common.retention`, section 6.6).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from rhi_common import retention
from rhi_common.keys import generate_key, key_hash
from rhi_common.students import RosterEntry, StudentKey


@dataclass(frozen=True)
class Minted:
    """A new key, in clear, and what the login shows with it. Times are whole seconds since the epoch."""

    key: str
    issued_at: int
    expires_at: int
    previous_issued_at: int | None


def expiry(course_end: datetime.date) -> int:
    """When a key of a course that ends on that date stops working: the end of that day, in UTC.

    It is the expiry of the key's item too, so it is held to the retention of personal rows.
    """
    day_after = course_end + datetime.timedelta(days=1)
    end_of_day = int(datetime.datetime.combine(day_after, datetime.time.min, datetime.UTC).timestamp())
    return min(end_of_day, retention.expires_at(course_end))


def held_to_validity(expires_at: int, course_end: datetime.date) -> int:
    """The expiry of an active key written again: its own, or `expiry(course_end)` when that is earlier.

    A key's validity ends at the course end (section 6.4), so a key still in use that is written
    again after the course end was moved earlier stops working by the new date; an earlier expiry
    it already had stays.
    """
    return min(int(expires_at), expiry(course_end))


def mint(entry: RosterEntry, installation: str, *, course_end: datetime.date, now: int) -> tuple[str, StudentKey]:
    """A new key for the student of that roster entry, and the item that stores it as its SHA-256.

    `installation` is the identifier the CLI sent with the device code, and `now` the time of issue
    in whole seconds since the epoch.
    """
    key = generate_key()
    record = StudentKey(
        key_hash=key_hash(key),
        github_id=entry.github_id,
        alias=entry.alias,
        installation=installation,
        issued_at=now,
        expires_at=expiry(course_end),
        revoked_at=None,
        concurrency_limit=entry.concurrency_limit,
    )
    return key, record
