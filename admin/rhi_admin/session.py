"""The pools' sessions and the session switch, which only the admin sets (system design, section 5.6).

**A pool's session** is a booking of the pool: a number of instances, a start and an end, in one
item of the pool-state table (`rhi_common.records.Session`), which the scaler reads on every run.
The commands write it with the rules of `rhi_common.sessions`, the same for every writer:

- `start`: books the pool (`started`);
- `change`, the command `set`: gives the session what it is given and keeps the rest (`changed`);
- `stop`: ends the session now (`stopped`), and `stop_all` every deployed pool's, one pool at a
  time, so that what was written is known when a later pool's write fails. A pool with no session
  booked or active has nothing to stop: nothing is written, and that is no refusal;
- `supply`, the pools' lines of `session status`: for each deployed pool, its session if one is
  booked or active, and what the scaler wrote in its note (`rhi_common.records.ScalerNote`): the
  instances requested, asked of AWS, waiting and since when, starting and in service, and why the
  last launch failed.

A refusal of those rules (`rhi_common.sessions.SessionError`) is the command's, with its reason.
A write holds only if the item still reads what the command read, no item or the same instances,
start, end, who and when: otherwise the command reads the item again and decides again, so it
never writes over a change it did not see. After a write, `applied` asks the scaler for a run, so
that the write applies within seconds. The scaler runs one invocation at a time: a run under way
refuses a second one, which fails nothing, since the next run is within a minute.

The bounds come from where the running system reads them: a pool's `max_instances` from the
settings of the scaler's function (`rhi_admin.pools.max_instances`), and `session_max_hours`, the
`time_zone` and the default pool from the configuration in SSM. A parked stage holds no scaler, so
no pool is deployed: `supply` has no line, and `start`, `change` and `stop` are refused.

**Times and lengths.** `read_time` reads a time as the command line gives it: `HH:MM` is the next
time the clock of the configured zone shows that hour and minute, counted from the moment of the
command, and `YYYY-MM-DDTHH:MM` is that date and time in the zone. A time is one the clock shows:
on the day it is set back, the second 02:30 is still ahead once the first has passed, and on the
day it is set forward there is no 02:30, so the next one is the day after's and that day's, written
with its date, is refused. `read_length` reads a length: a
whole number followed by `m` for minutes or `h` for hours. `show_time` writes a time back in the
second form.

**The switch.** One item of the pool-state table says whether a session is on:
`rhi_common.records.SessionSwitch`, under the keys `rhi_common.tables.SESSION_SWITCH_KEY`, which no
pool's name can take. The gateway serves students only while it reads on, and the pipeline's
deployment waits while it does. No deployment writes it, and a missing item reads as off.

- `status`: whether the session is on, and who last switched it and when;
- `turn`: the session on or off, with who and when. Switching it to what it already reads writes
  nothing (`rhi_common.records.switched`), so the time an off switch carries is the time the last
  session ended. The write holds only if the switch still reads what was read before it: when two
  admins switch it at once, the first transition keeps its who and when, and the other command
  reads it again and finds it already so.

Who wrote a session or switched the switch is the caller's identity without its account
(`rhi_admin.pools.identity_name`).
"""

from __future__ import annotations

import dataclasses
import datetime
import re
import zoneinfo
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from rhi_admin import pools as admin_pools
from rhi_admin.roster import AdminError
from rhi_common import sessions as rules
from rhi_common.records import (
    NOTE_RECORD,
    SESSION_RECORD,
    RecordError,
    ScalerNote,
    Session,
    SessionSwitch,
    switched,
)
from rhi_common.tables import SESSION_SWITCH_KEY

MINUTE_SECONDS = 60
# How a time is written with its date, on the command line and in the pools' lines of the status.
DATE_AND_TIME = "%Y-%m-%dT%H:%M"
_TIME = re.compile(r"(\d{2}):(\d{2})")
_DATE_AND_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_LENGTH = re.compile(r"(\d+)([mh])")
# How many days `HH:MM` is looked for, from today: a clock change skips it on one day at most.
_DAYS_AHEAD = 7
# What compares an item with the session that was read: each attribute of the record but its key.
_COMPARED = {"#instances": "instances", "#starts": "starts_at", "#ends": "ends_at", "#by": "set_by", "#at": "set_at"}


@dataclass(frozen=True)
class Switch:
    """What the commands act on: the pool-state table (a boto3 table resource) and the name of whoever runs them."""

    table: Any
    caller: Callable[[], str]


def status(switch: Switch) -> SessionSwitch | None:
    """The switch as it is stored; `None` when it has no item, which reads as off."""
    item = switch.table.get_item(Key=SESSION_SWITCH_KEY, ConsistentRead=True).get("Item")
    if item is None:
        return None
    try:
        return SessionSwitch.from_item(item)
    except RecordError as error:
        raise AdminError(f"the session switch does not read: {error}") from None


def _unchanged_since(current: SessionSwitch | None) -> dict[str, Any]:
    """The condition that the switch still reads `current`: a missing item, or the same who and when."""
    if current is None:
        return {"ConditionExpression": "attribute_not_exists(#pool)", "ExpressionAttributeNames": {"#pool": "pool"}}
    return {
        "ConditionExpression": "#on = :on AND #by = :by AND #at = :at",
        "ExpressionAttributeNames": {"#on": "on", "#by": "set_by", "#at": "set_at"},
        "ExpressionAttributeValues": {":on": current.on, ":by": current.set_by, ":at": current.set_at},
    }


def turn(on: bool, *, switch: Switch, now: int) -> tuple[SessionSwitch | None, bool]:
    """The session switched `on` or off; the switch it then reads, and whether anything was written."""
    while True:
        current = status(switch)
        changed = switched(current, on, switch.caller(), now)
        if changed is None:
            return current, False
        try:
            switch.table.put_item(Item=changed.to_item(), **_unchanged_since(current))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            continue  # another admin switched it between the read and the write: read it again
        return changed, True


@dataclass(frozen=True)
class Sessions:
    """What the commands on a pool's session act on: the deployed pools, and what the configuration
    in SSM gives them, its time zone, the longest a session may last and its default pool."""

    pools: admin_pools.Pools
    time_zone: str
    session_max_hours: int
    default_pool: str | None


@dataclass(frozen=True)
class Supply:
    """One pool as `session status` shows it. `session` is `None` when the pool has none booked or
    active, and its `ends_at` is the end every reader takes (`rhi_common.sessions.end`). `note` is
    what the scaler last wrote, zeros when it has written nothing."""

    pool: str
    session: Session | None
    active: bool
    note: ScalerNote


def _shown(local: datetime.datetime, zone: zoneinfo.ZoneInfo) -> list[int]:
    """The moments at which the clock of the zone shows `local`, in order: one, none when the clock
    is set forward over it, or two when the clock is set back and shows it again."""
    moments = set()
    for fold in (0, 1):
        at = local.replace(tzinfo=zone, fold=fold)
        # The clock shows `local` at that moment only if the moment reads back as `local`.
        if at.astimezone(datetime.UTC).astimezone(zone).replace(tzinfo=None, fold=0) == local:
            moments.add(int(at.timestamp()))
    return sorted(moments)


def read_time(text: str, *, time_zone: str, now: int) -> int:
    """The time `text` names, in seconds since the epoch: `HH:MM`, the next time the clock of the
    zone shows it at or after `now`, or `YYYY-MM-DDTHH:MM`, that date and time in the zone.

    Where the clock is set back and shows a time twice, the time is the first of the two that has
    not passed. Where it is set forward, it never shows the times it skips: `HH:MM` is then the
    next day's, and a date and time is refused."""
    zone = zoneinfo.ZoneInfo(time_zone)
    not_a_time = AdminError(f"{text!r} is not a time: write HH:MM, or YYYY-MM-DDTHH:MM")
    clock = _TIME.fullmatch(text)
    if clock is None:
        if not _DATE_AND_TIME.fullmatch(text):
            raise not_a_time
        try:
            dated = datetime.datetime.strptime(text, DATE_AND_TIME)
        except ValueError:
            raise not_a_time from None
        moments = _shown(dated, zone)
        if not moments:
            raise AdminError(f"the clock of {time_zone} never shows {text}: it is set forward over that time")
        return next((at for at in moments if at >= now), moments[0])
    try:
        shown = datetime.time(int(clock[1]), int(clock[2]))
    except ValueError:
        raise not_a_time from None
    today = datetime.datetime.fromtimestamp(now, zone).date()
    # Today's, tomorrow's when the clock has passed it, the day after's when tomorrow's clock skips it.
    for ahead in range(_DAYS_AHEAD):
        day = today + datetime.timedelta(days=ahead)
        for at in _shown(datetime.datetime.combine(day, shown), zone):
            if at >= now:
                return at
    raise AdminError(f"the clock of {time_zone} does not show {text} in the next {_DAYS_AHEAD} days")


def read_length(text: str) -> int:
    """The length `text` names, in seconds: a whole number followed by `m` (minutes) or `h` (hours)."""
    length = _LENGTH.fullmatch(text)
    if length is None:
        raise AdminError(f"{text!r} is not a length: write a whole number followed by m or h, as 90m or 6h")
    return int(length[1]) * (MINUTE_SECONDS if length[2] == "m" else rules.HOUR_SECONDS)


def show_time(at: int, *, time_zone: str) -> str:
    """The time `at`, in seconds since the epoch, as the clock of the zone shows it: `YYYY-MM-DDTHH:MM`."""
    return datetime.datetime.fromtimestamp(at, zoneinfo.ZoneInfo(time_zone)).strftime(DATE_AND_TIME)


def pool_of(given: str | None, sessions: Sessions) -> str:
    """The pool a command acts on: the one it was given, or the configuration's default pool."""
    if given is not None:
        return given
    if sessions.default_pool is None:
        raise AdminError("the configuration has no default pool: name one with --pool")
    return sessions.default_pool


def _max_instances(pool: str, sessions: Sessions) -> int:
    """The most a session may book of the pool. A pool that is not deployed is refused."""
    deployed = admin_pools.max_instances(sessions.pools)
    if pool not in deployed:
        raise admin_pools.not_deployed(pool, sessions.pools)
    return deployed[pool]


def _session(item: dict[str, Any] | None, pool: str) -> Session | None:
    if item is None:
        return None
    try:
        return Session.from_item(item)
    except RecordError as error:
        raise AdminError(f"the session of the pool {pool!r} does not read: {error}") from None


def _still(current: Session | None) -> dict[str, Any]:
    """The condition that the item still reads `current`: no item, or one that reads the same."""
    if current is None:
        return {"ConditionExpression": "attribute_not_exists(#pool)", "ExpressionAttributeNames": {"#pool": "pool"}}
    item = current.to_item()
    return {
        "ConditionExpression": " AND ".join(f"{name} = :{attribute}" for name, attribute in _COMPARED.items()),
        "ExpressionAttributeNames": dict(_COMPARED),
        "ExpressionAttributeValues": {f":{attribute}": item[attribute] for attribute in _COMPARED.values()},
    }


def _write[Written: Session | None](pool: str, sessions: Sessions, rule: Callable[[Session | None], Written]) -> Written:
    """Writes what `rule` gives for the pool's session as it is read, only if the item still reads
    so; the session written, or `None` when the rule has nothing to write. A refusal of the rule is
    the command's."""
    table = sessions.pools.table
    while True:
        item = table.get_item(Key={"pool": pool, "record": SESSION_RECORD}, ConsistentRead=True).get("Item")
        current = _session(item, pool)
        try:
            session = rule(current)
        except rules.SessionError as error:
            raise AdminError(str(error)) from None
        if session is None:
            return session
        try:
            table.put_item(Item=session.to_item(), **_still(current))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            continue  # someone wrote the session between the read and the write: read it again
        return session


def start(pool: str, *, instances: int, starts_at: int, ends_at: int, sessions: Sessions, now: int) -> Session:
    """Books the pool: `instances` instances from `starts_at` to `ends_at`. The session written."""
    most, who = _max_instances(pool, sessions), sessions.pools.caller()

    def rule(current: Session | None) -> Session:
        return rules.started(
            current,
            pool=pool,
            instances=instances,
            starts_at=starts_at,
            ends_at=ends_at,
            set_by=who,
            now=now,
            max_instances=most,
            session_max_hours=sessions.session_max_hours,
        )

    return _write(pool, sessions, rule)


def change(
    pool: str,
    *,
    sessions: Sessions,
    now: int,
    instances: int | None = None,
    starts_at: int | None = None,
    ends_at: int | None = None,
    length: int | None = None,
) -> Session:
    """Changes the pool's session: what is given replaces the session's, and the rest is kept. The
    end is `ends_at`, or `length` seconds after the later of the session's start and `now`. The
    session written."""
    most, who = _max_instances(pool, sessions), sessions.pools.caller()

    def rule(current: Session | None) -> Session:
        end = ends_at
        if length is not None:
            kept = now if current is None else current.starts_at
            end = max(now, kept if starts_at is None else starts_at) + length
        return rules.changed(
            current,
            set_by=who,
            now=now,
            max_instances=most,
            session_max_hours=sessions.session_max_hours,
            instances=instances,
            starts_at=starts_at,
            ends_at=end,
        )

    return _write(pool, sessions, rule)


def _stop(pool: str, sessions: Sessions, now: int) -> Session | None:
    who = sessions.pools.caller()
    return _write(
        pool,
        sessions,
        lambda current: rules.stopped(current, set_by=who, now=now, session_max_hours=sessions.session_max_hours),
    )


def stop(pool: str, *, sessions: Sessions, now: int) -> Session | None:
    """Ends the pool's session now: the session written, or `None` when the pool has none booked or
    active, and nothing was written."""
    _max_instances(pool, sessions)
    return _stop(pool, sessions, now)


def stop_all(*, sessions: Sessions, now: int) -> Iterator[tuple[str, Session | None]]:
    """Ends every deployed pool's session now, by name: each pool with what `stop` gives, as it is
    done. When a pool's write fails, the pools given before it are the ones that were stopped."""
    for pool in sorted(admin_pools.max_instances(sessions.pools)):
        yield pool, _stop(pool, sessions, now)


def applied(sessions: Sessions) -> bool:
    """Asks the scaler for a run, so that a write applies within seconds; whether it ran. It did
    not when a run is under way, which refuses a second one: the next run is within a minute. A
    run that fails, or that Lambda throttles for another reason, is the command's failure."""
    try:
        admin_pools.run_scaler(pools=sessions.pools)
    except admin_pools.ScalerBusy:
        return False
    return True


def supply(sessions: Sessions, *, now: int) -> list[Supply]:
    """Every deployed pool, by name: its session and what the scaler's note says of its instances."""
    found = []
    for pool in sorted(admin_pools.max_instances(sessions.pools)):
        answer = sessions.pools.table.query(
            KeyConditionExpression="#pool = :pool",
            ExpressionAttributeNames={"#pool": "pool"},
            ExpressionAttributeValues={":pool": pool},
            ConsistentRead=True,
        )
        records = {item.get("record"): item for item in answer.get("Items", [])}
        session = _session(records.get(SESSION_RECORD), pool)
        if session is not None:
            end = rules.end(session, session_max_hours=sessions.session_max_hours)
            session = dataclasses.replace(session, ends_at=end) if now < end else None
        note = ScalerNote(pool=pool, capacity_asked=0, no_capacity_at=None, written_at=0)
        if NOTE_RECORD in records:
            try:
                note = ScalerNote.from_item(records[NOTE_RECORD])
            except RecordError as error:
                raise AdminError(f"the scaler's note of the pool {pool!r} does not read: {error}") from None
        active = rules.is_active(session, now, session_max_hours=sessions.session_max_hours)
        found.append(Supply(pool, session, active, note))
    return found
