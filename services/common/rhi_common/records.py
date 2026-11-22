"""The rows the gateway, the instances and the scaler exchange (system design, section 5.1).

The scaler decides from rows that other components write, and the gateway reads what the scaler
and the admin leave for it. Three packages read and write these rows, so their shape is decided
here and nowhere else. Nothing here calls AWS: a record converts to and from an item, and whoever
holds a table reads and writes it.

| Table | Partition key | Sort key | Items |
| --- | --- | --- | --- |
| pool-state | `pool` | `record` | `mode`, the admin's record: `PoolState`. `note`, the scaler's: `ScalerNote`. Under the pool `_session`, `switch`: `SessionSwitch` |
| heartbeats | `pool` | `source` | `gateway#<task ID>`: `GatewayHeartbeat`. `instance#<instance ID>`: `InstanceHeartbeat` |

An item is in the form boto3's table resource takes and returns: attribute names to plain values,
strings and whole numbers. `to_item` writes every number as an `int`; `from_item` also takes the
`Decimal` that boto3 gives back for a number. A time is a whole number of seconds since the epoch.
A time that may be unknown (no request received yet, no launch that found no capacity) is `None`
in the record and absent from the item. A pool's rebalance number is absent from the scaler's note
while it is 0, and a note stored without one reads as 0.

A heartbeat is rewritten every 5 seconds and ignored once it is older than 20 seconds (section
17): `is_fresh` is that rule. Its item also carries `expires_at`, the table's expiry attribute, one
hour after it was written, which only clears the rows of tasks and instances that are gone. The
pool-state items carry no expiry: they survive deployments (section 11.3).

**The session switch** (section 5.6) is one item of the pool-state table, beside the pools' records
under a key no pool can have: partition key `_session`, sort key `switch`. A pool's name is
lower-case letters, digits and hyphens, so `_session` is never one. The item says whether a session
is on (`on`, a boolean), who set it and when; the admin sets it, no deployment writes it, and a
missing item reads as off (`session_is_on`).

`from_item` refuses, with a `RecordError`, an item that is not the record asked for: another key,
a missing or mistyped attribute, a mode outside the four, a switch whose `on` is not a boolean.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Self

# The sort keys of the pool-state table that this module owns.
MODE_RECORD = "mode"
NOTE_RECORD = "note"
# The keys of the session switch's item in the pool-state table: a partition no pool can take.
SESSION_POOL = "_session"
SWITCH_RECORD = "switch"
# What comes before the task's or the instance's ID in the sort key of the heartbeats table.
GATEWAY_SOURCE = "gateway#"
INSTANCE_SOURCE = "instance#"

# Readers ignore a heartbeat older than this (section 17).
FRESH_SECONDS = 20
# How long after it was written a heartbeat's row may be deleted. The design gives no figure.
EXPIRY_SECONDS = 3600

type Item = dict[str, str | int]


class RecordError(ValueError):
    """An item is not the record it was read as."""


class Mode(StrEnum):
    """A pool's operating state (section 11.3). `parked` is a deployment flag, not a mode."""

    ENABLED = "enabled"
    SCHEDULED_ONLY = "scheduled-only"
    DISABLED = "disabled"
    BUDGET_LATCHED = "budget-latched"


# The mode of a pool that has no mode record, by the `wake` of its configuration (section 11.3):
# what the scaler reads, and what the admin CLI re-enables a pool to.
CONFIGURED_MODE = {"on-demand": Mode.ENABLED, "scheduled-only": Mode.SCHEDULED_ONLY}


def _text(item: Mapping[str, object], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        raise RecordError(f"`{name}` is not a non-empty string")
    return value


def _whole(item: Mapping[str, object], name: str) -> int:
    """A count or a time: a whole number that is not negative, as an `int`."""
    value = item.get(name)
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecordError(f"`{name}` is not a whole number, zero or more")
    return value


def _optional_time(item: Mapping[str, object], name: str) -> int | None:
    return None if item.get(name) is None else _whole(item, name)


def _record(item: Mapping[str, object], record: str) -> None:
    if item.get("record") != record:
        raise RecordError(f"the item is not the record `{record}`")


def _source_id(item: Mapping[str, object], prefix: str) -> str:
    """The task's or the instance's ID, from a sort key that starts with the prefix."""
    source = _text(item, "source")
    if not source.startswith(prefix) or source == prefix:
        raise RecordError(f"`source` is not `{prefix}` followed by an ID")
    return source.removeprefix(prefix)


@dataclass(frozen=True)
class GatewayHeartbeat:
    """One gateway task's demand for one pool, rewritten every 5 seconds.

    An explicit wake counts as a held request until the pool is ready, and as a request received
    at that moment. The rule that takes a pool to zero counts from `last_request_at`.
    """

    task: str
    pool: str
    in_flight: int
    held: int
    last_request_at: int | None
    written_at: int

    def to_item(self) -> Item:
        item: Item = {
            "pool": self.pool,
            "source": GATEWAY_SOURCE + self.task,
            "in_flight": self.in_flight,
            "held": self.held,
            "written_at": self.written_at,
            "expires_at": self.written_at + EXPIRY_SECONDS,
        }
        if self.last_request_at is not None:
            item["last_request_at"] = self.last_request_at
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        return cls(
            task=_source_id(item, GATEWAY_SOURCE),
            pool=_text(item, "pool"),
            in_flight=_whole(item, "in_flight"),
            held=_whole(item, "held"),
            last_request_at=_optional_time(item, "last_request_at"),
            written_at=_whole(item, "written_at"),
        )


@dataclass(frozen=True)
class InstanceHeartbeat:
    """One instance's engine load and the release it serves, written by its lifecycle agent.

    The release is the engine image digest and the model revision (sections 4.1 and 4.3). `stage`
    is the last lifecycle stage the instance reached, a name of the lifecycle agent's own list.
    """

    instance: str
    pool: str
    running: int
    waiting: int
    image_digest: str
    model_revision: str
    stage: str
    written_at: int

    def to_item(self) -> Item:
        return {
            "pool": self.pool,
            "source": INSTANCE_SOURCE + self.instance,
            "running": self.running,
            "waiting": self.waiting,
            "image_digest": self.image_digest,
            "model_revision": self.model_revision,
            "stage": self.stage,
            "written_at": self.written_at,
            "expires_at": self.written_at + EXPIRY_SECONDS,
        }

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        return cls(
            instance=_source_id(item, INSTANCE_SOURCE),
            pool=_text(item, "pool"),
            running=_whole(item, "running"),
            waiting=_whole(item, "waiting"),
            image_digest=_text(item, "image_digest"),
            model_revision=_text(item, "model_revision"),
            stage=_text(item, "stage"),
            written_at=_whole(item, "written_at"),
        )


@dataclass(frozen=True)
class PoolState:
    """A pool's mode, and who set it and when. Only the admin changes it."""

    pool: str
    mode: Mode
    set_by: str
    set_at: int

    def to_item(self) -> Item:
        return {
            "pool": self.pool,
            "record": MODE_RECORD,
            "mode": self.mode.value,
            "set_by": self.set_by,
            "set_at": self.set_at,
        }

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        _record(item, MODE_RECORD)
        try:
            mode = Mode(item.get("mode"))
        except ValueError:
            raise RecordError("`mode` is not one of the four modes") from None
        return cls(pool=_text(item, "pool"), mode=mode, set_by=_text(item, "set_by"), set_at=_whole(item, "set_at"))


@dataclass(frozen=True)
class ScalerNote:
    """What only the scaler knows about a pool and every gateway task reads (section 5.3).

    `capacity_asked` is the number of instances the scaler last asked the pool's group for: zero
    says it asked for none. `no_capacity_at` is when a launch last found no capacity, `None` if
    none did.

    `rebalance_number` is the pool's rebalance number (section 5.5): the scaler raises it while the
    pool is lopsided, and for each new number the gateway moves a share of the pool's students. It
    is 0 until the scaler first raises it, and the item holds it only once it is above 0.
    """

    pool: str
    capacity_asked: int
    no_capacity_at: int | None
    written_at: int
    rebalance_number: int = 0

    def to_item(self) -> Item:
        item: Item = {
            "pool": self.pool,
            "record": NOTE_RECORD,
            "capacity_asked": self.capacity_asked,
            "written_at": self.written_at,
        }
        if self.no_capacity_at is not None:
            item["no_capacity_at"] = self.no_capacity_at
        if self.rebalance_number > 0:
            item["rebalance_number"] = self.rebalance_number
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        _record(item, NOTE_RECORD)
        return cls(
            pool=_text(item, "pool"),
            capacity_asked=_whole(item, "capacity_asked"),
            no_capacity_at=_optional_time(item, "no_capacity_at"),
            written_at=_whole(item, "written_at"),
            # A note written before the scaler raised a number, or before it kept one, holds none.
            rebalance_number=0 if item.get("rebalance_number") is None else _whole(item, "rebalance_number"),
        )


@dataclass(frozen=True)
class SessionSwitch:
    """Whether a session is on, and who set the switch and when. Only the admin changes it."""

    on: bool
    set_by: str
    set_at: int

    def to_item(self) -> Item:
        return {"pool": SESSION_POOL, "record": SWITCH_RECORD, "on": self.on, "set_by": self.set_by, "set_at": self.set_at}

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        _record(item, SWITCH_RECORD)
        if item.get("pool") != SESSION_POOL:
            raise RecordError(f"the item is not under the pool `{SESSION_POOL}`")
        on = item.get("on")
        if not isinstance(on, bool):
            raise RecordError("`on` is not a boolean")
        return cls(on=on, set_by=_text(item, "set_by"), set_at=_whole(item, "set_at"))


def session_is_on(item: Mapping[str, object] | None) -> bool:
    """Whether the switch's item, as read, says a session is on: a missing item reads as off."""
    return item is not None and SessionSwitch.from_item(item).on


def is_fresh(heartbeat: GatewayHeartbeat | InstanceHeartbeat, now: int) -> bool:
    """Whether a reader at the time `now` still counts the heartbeat: not once it is older than 20 seconds."""
    return now - heartbeat.written_at <= FRESH_SECONDS
