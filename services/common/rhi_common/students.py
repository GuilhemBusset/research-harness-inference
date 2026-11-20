"""A student's key, roster entry and day of usage (system design, sections 6.2, 6.4 and 6.6).

The issuer and the admin CLI write keys, the admin CLI keeps the roster, the gateway reads a key on
every request and reserves and settles budget, and the export, erasure and retention commands read
all three. Five components write the same items, so their attributes are decided here and nowhere
else. Nothing here calls AWS: an item converts to and from its record, and whoever holds a table
reads and writes it. The tables and their keys are in `rhi_common.tables`.

| Table | Item | Attributes |
| --- | --- | --- |
| keys | `StudentKey` | `key_hash`, `github_id`, `alias`, `installation`, `issued_at`, `expires_at`, `revoked_at`, `daily_token_budget`, `concurrency_limit` |
| roster | `RosterEntry` | `github_id`, `alias`, `daily_token_budget`, `concurrency_limit`, `added_at`, `expires_at` |
| usage | `UsageDay` | `github_id`, `day`, `alias`, `tokens`, `leases`, `expires_at`, `affinity_cookies`, `rebalance_applied` |

A lease, one value of the map `leases`, holds `tokens`, `estimate` and `expires`.

These are the only attributes an item of this module writes. None can hold a key in clear, a GitHub
token, a request or a response: a key is stored as its SHA-256 only (`rhi_common.keys.key_hash`),
and `key_hash` is refused unless it has the form of one.

An item is in the form boto3's table resource takes and returns, as in `rhi_common.records`:
`to_item` writes every number as an `int`, `from_item` also takes the `Decimal` boto3 gives back,
a time is a whole number of seconds since the epoch, and what is not known is `None` or empty in
the record and absent from the item, except `leases`, which is written even when empty.
`from_item` refuses, with a `RecordError`, an item with a missing or mistyped attribute.

`expires_at` is the attribute the table deletes an item by, and whoever writes the item sets it.
On a key it is the course end, and the key is refused after it whatever the table has deleted.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Self

from rhi_common.records import RecordError, _optional_time, _text, _whole

type Item = dict[str, str | int | dict[str, str] | dict[str, int] | dict[str, dict[str, int]]]

_KEY_HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def utc_day(at: int) -> str:
    """The sort key of the usage table for a time: its UTC date, as YYYY-MM-DD."""
    return datetime.fromtimestamp(at, UTC).date().isoformat()


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
    budget and the concurrency limit are the roster entry's when the key was issued, so that the
    gateway reads one item per request.
    """

    key_hash: str
    github_id: int
    alias: str
    installation: str | None
    issued_at: int
    expires_at: int
    revoked_at: int | None
    daily_token_budget: int
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
            "daily_token_budget": self.daily_token_budget,
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
            daily_token_budget=_whole(item, "daily_token_budget"),
            concurrency_limit=_whole(item, "concurrency_limit"),
        )


@dataclass(frozen=True)
class RosterEntry:
    """One student: the GitHub ID that a handle cannot change, and the alias metrics and logs use."""

    github_id: int
    alias: str
    daily_token_budget: int
    concurrency_limit: int
    added_at: int
    expires_at: int

    def to_item(self) -> Item:
        return {
            "github_id": self.github_id,
            "alias": self.alias,
            "daily_token_budget": self.daily_token_budget,
            "concurrency_limit": self.concurrency_limit,
            "added_at": self.added_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        return cls(
            github_id=_whole(item, "github_id"),
            alias=_text(item, "alias"),
            daily_token_budget=_whole(item, "daily_token_budget"),
            concurrency_limit=_whole(item, "concurrency_limit"),
            added_at=_whole(item, "added_at"),
            expires_at=_whole(item, "expires_at"),
        )


@dataclass(frozen=True)
class Lease:
    """One request's concurrency slot: the tokens it reserved, the estimate of its input, its expiry."""

    tokens: int
    estimate: int
    expires: int

    def to_map(self) -> dict[str, int]:
        return {"tokens": self.tokens, "estimate": self.estimate, "expires": self.expires}

    @classmethod
    def from_map(cls, value: object) -> Self:
        if not isinstance(value, Mapping):
            raise RecordError("a lease is not a map")
        return cls(tokens=_whole(value, "tokens"), estimate=_whole(value, "estimate"), expires=_whole(value, "expires"))


@dataclass(frozen=True)
class UsageDay:
    """One student's day: the tokens reserved and charged, and the leases of the requests in flight.

    The budget and the concurrency slots live in one item so that one conditional write reserves
    against both (section 6.2, step 3). `leases` maps a request's ID to its lease, and a lease
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
    tokens: int
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
            "tokens": self.tokens,
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
            tokens=_whole(item, "tokens"),
            expires_at=_whole(item, "expires_at"),
            leases=_map(item, "leases", lambda leases, request: Lease.from_map(leases[request])),
            affinity_cookies=_map(item, "affinity_cookies", _text),
            rebalance_applied=_map(item, "rebalance_applied", _whole),
        )
