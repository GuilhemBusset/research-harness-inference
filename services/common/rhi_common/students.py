"""A student's key, roster entry and day of usage (system design, sections 6.2, 6.4 and 6.6).

The issuer and the admin CLI write keys, the admin CLI keeps the roster, the gateway reads a key on
every request and takes and releases a concurrency slot, and the export, erasure and retention commands read
all three. Five components write the same items, so their attributes are decided here and nowhere
else. Nothing here calls AWS: an item converts to and from its record, and whoever holds a table
reads and writes it. The tables and their keys are in `rhi_common.tables`.

| Table | Item | Attributes |
| --- | --- | --- |
| keys | `StudentKey` | `key_hash`, `github_id`, `alias`, `installation`, `issued_at`, `expires_at`, `revoked_at`, `concurrency_limit` |
| roster | `RosterEntry` | `github_id`, `alias`, `concurrency_limit`, `added_at`, `expires_at` |
| usage | `UsageDay` | `github_id`, `day`, `alias`, `leases`, `expires_at`, `affinity_cookies`, `rebalance_applied` |

A lease, one value of the map `leases`, holds `expires`.

These are the only attributes an item of this module writes. None can hold a key in clear, a GitHub
token, a request or a response: a key is stored as its SHA-256 only (`rhi_common.keys.key_hash`),
and `key_hash` is refused unless it has the form of one.

An item is in the form boto3's table resource takes and returns, as in `rhi_common.records`:
`to_item` writes every number as an `int`, `from_item` also takes the `Decimal` boto3 gives back,
a time is a whole number of seconds since the epoch, and what is not known is `None` or empty in
the record and absent from the item, except `leases`, which is written even when empty.
`from_item` refuses, with a `RecordError`, an item with a missing or mistyped attribute.

There is no per-student token budget (ADR 0003, decisions 2 and 3). An item written by an earlier
release can still hold a key's or a roster entry's `daily_token_budget`, a day's `tokens` or a
lease's `tokens` and `estimate`, until it expires. No record has them and nothing here writes them:
`from_item` and `from_map` read only the attributes above and ignore any other, so such an item
reads as one without them. `without_budget` gives an item as it is stored less those attributes
alone, for a reader that keeps what else the item holds (the privacy export).

`expires_at` is the attribute the table deletes an item by, and whoever writes the item sets it.
On a key it is the course end, and the key is refused after it whatever the table has deleted.

**The owner's own entries** are on the roster under GitHub IDs no GitHub account has, which GitHub
numbers upward from 1 and is far below 10^18: the canary of the pipeline's verification
(`CANARY_GITHUB_ID`, `rhi_admin.canary`) and the spikes' students, from `SPIKE_GITHUB_ID_BASE` on
(`rhi_admin.spikes`). The gateway serves them as it serves every key, on a pool whose session is
active (section 5.6): the verification, the weekly canary and the spikes book a session, or run
inside one. `is_owner_entry` says whether a GitHub ID is one of them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Self

from rhi_common.records import RecordError, _optional_time, _text, _whole

type Item = dict[str, str | int | dict[str, str] | dict[str, int] | dict[str, dict[str, int]]]

# GitHub numbers its accounts from 1: no student can have this ID.
CANARY_GITHUB_ID = 0
# The spikes' student n has the nth ID after this one; no student can have an ID from here on.
SPIKE_GITHUB_ID_BASE = 10**18

_KEY_HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def is_owner_entry(github_id: int) -> bool:
    """Whether a GitHub ID is one of the owner's own entries: the canary, or one of the spikes' students."""
    return github_id == CANARY_GITHUB_ID or github_id >= SPIKE_GITHUB_ID_BASE


def utc_day(at: int) -> str:
    """The sort key of the usage table for a time: its UTC date, as YYYY-MM-DD."""
    return datetime.fromtimestamp(at, UTC).date().isoformat()


_RETIRED = frozenset({"daily_token_budget", "tokens"})
_RETIRED_FROM_A_LEASE = frozenset({"tokens", "estimate"})


def without_budget(item: Mapping[str, object]) -> dict[str, object]:
    """A stored item less the budget's attributes an earlier release wrote, every other attribute kept as it is.

    It drops a key's or an entry's `daily_token_budget`, a day's `tokens`, and each lease's
    `tokens` and `estimate`; what the item holds besides, known here or not, stays.
    """
    kept = {name: value for name, value in item.items() if name not in _RETIRED}
    leases = kept.get("leases")
    if isinstance(leases, Mapping):
        kept["leases"] = {
            request: {name: value for name, value in lease.items() if name not in _RETIRED_FROM_A_LEASE}
            if isinstance(lease, Mapping)
            else lease
            for request, lease in leases.items()
        }
    return kept


def _optional_text(item: Mapping[str, object], name: str) -> str | None:
    return None if item.get(name) is None else _text(item, name)


def _map[Value](
    item: Mapping[str, object], name: str, read: Callable[[Mapping[str, object], str], Value]
) -> dict[str, Value]:
    """A map attribute, each value read as `read` reads an attribute; absent, it is empty."""
    value = item.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping) or not all(isinstance(key, str) and key for key in value):
        raise RecordError(f"`{name}` is not a map with non-empty names")
    return {key: read(value, key) for key in value}


@dataclass(frozen=True)
class StudentKey:
    """One API key, under the SHA-256 of the key; the key itself is never stored.

    `installation` is the identifier the CLI sends at login: `None` for a key issued without a
    login. `expires_at` is the course end. `revoked_at` is `None` until the key is revoked. The
    concurrency limit is the roster entry's when the key was issued, so that the gateway reads one
    item per request.
    """

    key_hash: str
    github_id: int
    alias: str
    installation: str | None
    issued_at: int
    expires_at: int
    revoked_at: int | None
    concurrency_limit: int

    def __post_init__(self) -> None:
        if _KEY_HASH.fullmatch(self.key_hash) is None:
            raise RecordError("`key_hash` is not a SHA-256 in lower-case hexadecimal")

    def to_item(self) -> Item:
        item: Item = {
            "key_hash": self.key_hash,
            "github_id": self.github_id,
            "alias": self.alias,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "concurrency_limit": self.concurrency_limit,
        }
        if self.installation is not None:
            item["installation"] = self.installation
        if self.revoked_at is not None:
            item["revoked_at"] = self.revoked_at
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        return cls(
            key_hash=_text(item, "key_hash"),
            github_id=_whole(item, "github_id"),
            alias=_text(item, "alias"),
            installation=_optional_text(item, "installation"),
            issued_at=_whole(item, "issued_at"),
            expires_at=_whole(item, "expires_at"),
            revoked_at=_optional_time(item, "revoked_at"),
            concurrency_limit=_whole(item, "concurrency_limit"),
        )


@dataclass(frozen=True)
class RosterEntry:
    """One student: the GitHub ID that a handle cannot change, and the alias metrics and logs use."""

    github_id: int
    alias: str
    concurrency_limit: int
    added_at: int
    expires_at: int

    def to_item(self) -> Item:
        return {
            "github_id": self.github_id,
            "alias": self.alias,
            "concurrency_limit": self.concurrency_limit,
            "added_at": self.added_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        return cls(
            github_id=_whole(item, "github_id"),
            alias=_text(item, "alias"),
            concurrency_limit=_whole(item, "concurrency_limit"),
            added_at=_whole(item, "added_at"),
            expires_at=_whole(item, "expires_at"),
        )


@dataclass(frozen=True)
class Lease:
    """One request's concurrency slot: when it expires."""

    expires: int

    def to_map(self) -> dict[str, int]:
        return {"expires": self.expires}

    @classmethod
    def from_map(cls, value: object) -> Self:
        if not isinstance(value, Mapping):
            raise RecordError("a lease is not a map")
        return cls(expires=_whole(value, "expires"))


@dataclass(frozen=True)
class UsageDay:
    """One student's day: the leases of the requests in flight, and what keeps the student on an instance.

    The concurrency slots live in one item so that one conditional write takes a slot against all
    of them (section 6.2, step 3). `leases` maps a request's ID to its lease, and a lease
    stays in the item of the day its request started. `affinity_cookies` maps a pool's name to the
    cookie the internal load balancer last set for this student, as it set it (section 5.5).
    `rebalance_applied` maps a pool's name to the last rebalance number of that pool the gateway
    applied to this student, a whole number, so that each number moves a student once at most
    (`rhi_gateway.affinity`). Each of the two maps is left out of the item form while it holds
    nothing, and an item without one reads as an empty map. The gateway's reservation leaves each
    of them empty in the table's item, for its settlement to store the cookie and the number in
    (`rhi_gateway.budget`): an empty map reads as none too.
    """

    github_id: int
    day: str
    alias: str
    expires_at: int
    leases: dict[str, Lease] = field(default_factory=dict)
    affinity_cookies: dict[str, str] = field(default_factory=dict)
    rebalance_applied: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            if _DAY.fullmatch(self.day) is None:
                raise ValueError
            date.fromisoformat(self.day)
        except ValueError:
            raise RecordError("`day` is not a date as YYYY-MM-DD") from None

    def to_item(self) -> Item:
        item: Item = {
            "github_id": self.github_id,
            "day": self.day,
            "alias": self.alias,
            # Written even when empty: a write that adds a lease needs the map to be there.
            "leases": {request: lease.to_map() for request, lease in self.leases.items()},
            "expires_at": self.expires_at,
        }
        if self.affinity_cookies:
            item["affinity_cookies"] = dict(self.affinity_cookies)
        if self.rebalance_applied:
            item["rebalance_applied"] = dict(self.rebalance_applied)
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        return cls(
            github_id=_whole(item, "github_id"),
            day=_text(item, "day"),
            alias=_text(item, "alias"),
            expires_at=_whole(item, "expires_at"),
            leases=_map(item, "leases", lambda leases, request: Lease.from_map(leases[request])),
            affinity_cookies=_map(item, "affinity_cookies", _text),
            rebalance_applied=_map(item, "rebalance_applied", _whole),
        )
