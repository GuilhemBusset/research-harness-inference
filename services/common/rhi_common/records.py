"""The rows the gateway, the instances and the scaler exchange (system design, section 5.1).

The scaler decides from rows that other components write, and the gateway reads what the scaler
and the admin leave for it. Three packages read and write these rows, so their shape is decided
here and nowhere else. Nothing here calls AWS: a record converts to and from an item, and whoever
holds a table reads and writes it.

| Table | Partition key | Sort key | Items |
| --- | --- | --- | --- |
| pool-state | `pool` | `record` | `note`, the scaler's: `ScalerNote`. `session`, the pool's booking: `Session` |
| heartbeats | `pool` | `source` | `instance#<instance ID>`: `InstanceHeartbeat` |

An item is in the form boto3's table resource takes and returns: attribute names to plain values,
strings and whole numbers. `to_item` writes every number as an `int`; `from_item` also takes the
`Decimal` that boto3 gives back for a number. A time is a whole number of seconds since the epoch.
A time that may be unknown (no launch that found no capacity yet) is `None`
in the record and absent from the item. A pool's rebalance number is absent from the scaler's note
while it is 0, and a note stored without one reads as 0. The note's counts of the pool's supply are
always written, and a note stored before the scaler wrote them reads as zeros, with no time and no
cause.

A heartbeat is rewritten every 5 seconds and ignored once it is older than 20 seconds (section
17): `is_fresh` is that rule. Its item also carries `expires_at`, the table's expiry attribute, one
hour after it was written, which only clears the rows of instances that are gone. The pool-state
items carry no expiry: they survive deployments (section 11.3).

`from_item` refuses, with a `RecordError`, an item that is not the record asked for: another key,
a missing or mistyped attribute.

The tables can still hold items of kinds an earlier release wrote and this module no longer
defines: in pool-state a pool's record `mode` and the item `switch` under `_session`, in heartbeats
the rows `gateway#<task ID>`. Nothing reads them and nothing deletes them; the heartbeat rows
expire by themselves.

A pool's session (section 5.6) is the record `session` under the pool's name: a booking of the
pool, with a number of instances, a start and an end. A pool has one at a time, and a pool with no
such item has none. `set_by` and `set_at` say who last wrote it and when. The record is only the
shape of the item: the bounds every reader and writer holds a session to are in
`rhi_common.sessions`. The scaler reads it on every run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Self

# The sort keys of the pool-state table that this module owns.
NOTE_RECORD = "note"
SESSION_RECORD = "session"
# What comes before the instance's ID in the sort key of the heartbeats table.
INSTANCE_SOURCE = "instance#"

# Readers ignore a heartbeat older than this (section 17).
FRESH_SECONDS = 20
# How long after it was written a heartbeat's row may be deleted. The design gives no figure.
EXPIRY_SECONDS = 3600

type Item = dict[str, str | int]


class RecordError(ValueError):
    """An item is not the record it was read as."""


class LaunchFailure(StrEnum):
    """Why a pool's group last failed to launch, while the pool waits (section 5.4). The scaler's
    note and its metrics carry one of these four words, never text from AWS."""

    # The launch found no capacity for the pool's instance types in its zones.
    INSUFFICIENT_CAPACITY = "insufficient-capacity"
    # The account's vCPU quota refused the launch.
    QUOTA = "quota"
    # The scaler suspended the group's launches, after three abandoned in a row.
    LAUNCHES_SUSPENDED = "launches-suspended"
    # The launch failed for any other reason.
    LAUNCH_FAILED = "launch-failed"


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


def _count(item: Mapping[str, object], name: str) -> int:
    """A count that an item stored before the count was written does not hold: it reads as 0."""
    return 0 if item.get(name) is None else _whole(item, name)


def _record(item: Mapping[str, object], record: str) -> None:
    if item.get("record") != record:
        raise RecordError(f"the item is not the record `{record}`")


def _source_id(item: Mapping[str, object], prefix: str) -> str:
    """The instance's ID, from a sort key that starts with the prefix."""
    source = _text(item, "source")
    if not source.startswith(prefix) or source == prefix:
        raise RecordError(f"`source` is not `{prefix}` followed by an ID")
    return source.removeprefix(prefix)


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
class ScalerNote:
    """What only the scaler knows about a pool, written on every run (sections 5.3 and 5.4). Every
    gateway task reads it, and so does the admin CLI.

    `capacity_asked` is the number of instances the scaler last asked the pool's group for, the
    "asked of AWS" of section 5.4: zero says it asked for none. `no_capacity_at` is when a launch
    last found no capacity, `None` if none did.

    `rebalance_number` is the pool's rebalance number (section 5.5): the scaler raises it while the
    pool is lopsided, and for each new number the gateway moves a share of the pool's students. It
    is 0 until the scaler first raises it, and the item holds it only once it is above 0.

    The rest is what the pool was asked and what waits (section 5.4). `requested` is what the pool's
    session asks for at the time of the run, 0 while none is active. `starting`, `in_service` and
    `draining` count the instances the run read in each state. `waiting` is what was asked of AWS
    and has no instance yet: `capacity_asked` less the instances starting and in service, never
    below zero. `waiting_since` is the time of the first of the runs in a row that saw `waiting`
    above zero, and `None` while nothing waits. `launch_failure` is why the group's last launch
    failed since then, and `None` while nothing waits or no launch has failed. The item always
    holds the five counts, and the time and the cause only while they are set. A note stored
    without them reads as zeros, no time and no cause.
    """

    pool: str
    capacity_asked: int
    no_capacity_at: int | None
    written_at: int
    rebalance_number: int = 0
    requested: int = 0
    starting: int = 0
    in_service: int = 0
    draining: int = 0
    waiting: int = 0
    waiting_since: int | None = None
    launch_failure: LaunchFailure | None = None

    def to_item(self) -> Item:
        item: Item = {
            "pool": self.pool,
            "record": NOTE_RECORD,
            "capacity_asked": self.capacity_asked,
            "written_at": self.written_at,
            "requested": self.requested,
            "starting": self.starting,
            "in_service": self.in_service,
            "draining": self.draining,
            "waiting": self.waiting,
        }
        if self.no_capacity_at is not None:
            item["no_capacity_at"] = self.no_capacity_at
        if self.rebalance_number > 0:
            item["rebalance_number"] = self.rebalance_number
        if self.waiting_since is not None:
            item["waiting_since"] = self.waiting_since
        if self.launch_failure is not None:
            item["launch_failure"] = self.launch_failure.value
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        _record(item, NOTE_RECORD)
        cause = item.get("launch_failure")
        try:
            launch_failure = None if cause is None else LaunchFailure(cause)
        except ValueError:
            raise RecordError("`launch_failure` is not one of the four causes") from None
        return cls(
            pool=_text(item, "pool"),
            capacity_asked=_whole(item, "capacity_asked"),
            no_capacity_at=_optional_time(item, "no_capacity_at"),
            written_at=_whole(item, "written_at"),
            # A note written before the scaler raised a number, or before it kept one, holds none.
            rebalance_number=_count(item, "rebalance_number"),
            # A note written before the scaler wrote these holds none of them.
            requested=_count(item, "requested"),
            starting=_count(item, "starting"),
            in_service=_count(item, "in_service"),
            draining=_count(item, "draining"),
            waiting=_count(item, "waiting"),
            waiting_since=_optional_time(item, "waiting_since"),
            launch_failure=launch_failure,
        )


@dataclass(frozen=True)
class Session:
    """A pool's session: a booking of the pool, and who last wrote it and when (section 5.6).

    `instances` is the number of instances booked, `starts_at` and `ends_at` the start and the end.
    The record holds what was written: `rhi_common.sessions` says when a session is active and how
    many instances it asks for, under the bounds of the configuration.
    """

    pool: str
    instances: int
    starts_at: int
    ends_at: int
    set_by: str
    set_at: int

    def to_item(self) -> Item:
        return {
            "pool": self.pool,
            "record": SESSION_RECORD,
            "instances": self.instances,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "set_by": self.set_by,
            "set_at": self.set_at,
        }

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> Self:
        _record(item, SESSION_RECORD)
        return cls(
            pool=_text(item, "pool"),
            instances=_whole(item, "instances"),
            starts_at=_whole(item, "starts_at"),
            ends_at=_whole(item, "ends_at"),
            set_by=_text(item, "set_by"),
            set_at=_whole(item, "set_at"),
        )


def is_fresh(heartbeat: InstanceHeartbeat, now: int) -> bool:
    """Whether a reader at the time `now` still counts the heartbeat: not once it is older than 20 seconds."""
    return now - heartbeat.written_at <= FRESH_SECONDS
