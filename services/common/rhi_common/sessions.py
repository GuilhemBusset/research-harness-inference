"""When a pool's session is active and what it asks for, and the bounds on writing one (system
design, section 5.6).

A session is a booking of one pool: a number of instances, a start and an end
(`rhi_common.records.Session`). It is booked from the moment it is written, and active from its
start to its end. A pool has one session at a time. Every reader and every writer of the record
calls the functions below, so a record written by any of them, or by hand, is read under the same
bounds. Nothing here calls anything: a function takes the record that was read, the time `now` in
whole seconds since the epoch, and the two bounds of the configuration, the pool's `max_instances`
and `session_max_hours`.

**Reading.** A session ends at its `ends_at`, or `session_max_hours` after the later of its
`starts_at` and its `set_at` when that comes first: a record that gives a later end is read as
ending there, and `end` gives that time. `is_active` is true from `starts_at` until that end, the
end itself excluded.
`requested` is the number of instances the pool is to hold: the session's `instances`, at most
`max_instances`, while it is active, and 0 before, after, and for a pool with no session.

**Writing.** `started`, `changed` and `stopped` return the session to write, with `set_by` and the
time `now` as who wrote it and when. Each refusal is a `SessionError`:

- `started` books a pool. It refuses a pool whose session is booked or active, a number of
  instances outside 1 to `max_instances`, a start before `now`, and an end that is not after the
  start or is more than `session_max_hours` after it.
- `changed` gives a booked or active session another number of instances, start or end, and keeps
  what it is not given. It refuses a pool with no session booked or active, a start moved once it
  has passed, a number of instances outside 1 to `max_instances`, and an end that is not after both
  `now` and the start or is more than `session_max_hours` after the later of the two. The bounds
  hold for the session it returns, so a value that is kept is checked like one that is given.
- `stopped` ends a booked or active session at `now`. It returns `None` for a pool with none: there
  is nothing to write.

A start has passed once `now` is after it: at the second of its start a session is active and its
start can still be moved. A session whose end has come is neither booked nor active, so its pool
can be booked again.
"""

from __future__ import annotations

import dataclasses

from rhi_common.records import Session

HOUR_SECONDS = 3600


class SessionError(ValueError):
    """A booking, or a change of one, is outside the bounds of a session."""


def _end(session: Session, session_max_hours: int) -> int:
    """When the session ends: its `ends_at`, or the longest a session may last when that comes first."""
    return min(session.ends_at, max(session.starts_at, session.set_at) + session_max_hours * HOUR_SECONDS)


def _booked_or_active(session: Session | None, now: int, session_max_hours: int) -> Session | None:
    """The session if its end has not come, `None` otherwise and for a pool with no session."""
    if session is None or now >= _end(session, session_max_hours):
        return None
    return session


def _bounded(session: Session, now: int, max_instances: int, session_max_hours: int) -> Session:
    """The session to write, or a `SessionError` when its instances or its end are outside the bounds."""
    if not 1 <= session.instances <= max_instances:
        raise SessionError("the number of instances is not from 1 to the pool's `max_instances`")
    later = max(now, session.starts_at)
    if session.ends_at <= later:
        raise SessionError("the end is not after both the start and the present moment")
    if session.ends_at > later + session_max_hours * HOUR_SECONDS:
        raise SessionError(
            "the end is more than `session_max_hours` after the later of the start and the present moment"
        )
    return session


def end(session: Session, *, session_max_hours: int) -> int:
    """When the session ends, as every reader takes it: its `ends_at`, or `session_max_hours` after
    the later of its start and the moment it was written when that comes first."""
    return _end(session, session_max_hours)


def is_active(session: Session | None, now: int, *, session_max_hours: int) -> bool:
    """Whether the session is active at the time `now`: from its start until its end, which is at
    most `session_max_hours` after the later of its start and the moment it was written."""
    return session is not None and session.starts_at <= now < _end(session, session_max_hours)


def requested(session: Session | None, now: int, *, max_instances: int, session_max_hours: int) -> int:
    """The instances the pool is to hold at the time `now`: the session's, at most `max_instances`,
    while it is active, and 0 otherwise."""
    if session is None or not is_active(session, now, session_max_hours=session_max_hours):
        return 0
    return min(session.instances, max_instances)


def started(
    current: Session | None,
    *,
    pool: str,
    instances: int,
    starts_at: int,
    ends_at: int,
    set_by: str,
    now: int,
    max_instances: int,
    session_max_hours: int,
) -> Session:
    """The session to write when `set_by` books the pool at the time `now`. `current` is the pool's
    record, `None` when it has none."""
    if _booked_or_active(current, now, session_max_hours) is not None:
        raise SessionError("the pool has a session booked or active")
    if starts_at < now:
        raise SessionError("the start is before the present moment")
    session = Session(pool=pool, instances=instances, starts_at=starts_at, ends_at=ends_at, set_by=set_by, set_at=now)
    return _bounded(session, now, max_instances, session_max_hours)


def changed(
    current: Session | None,
    *,
    set_by: str,
    now: int,
    max_instances: int,
    session_max_hours: int,
    instances: int | None = None,
    starts_at: int | None = None,
    ends_at: int | None = None,
) -> Session:
    """The session to write when `set_by` changes the pool's session at the time `now`: the values
    given replace the session's, and the others are kept."""
    session = _booked_or_active(current, now, session_max_hours)
    if session is None:
        raise SessionError("the pool has no session booked or active")
    if starts_at is not None and starts_at != session.starts_at and session.starts_at < now:
        raise SessionError("the start has passed and cannot be moved")
    wanted = Session(
        pool=session.pool,
        instances=session.instances if instances is None else instances,
        starts_at=session.starts_at if starts_at is None else starts_at,
        ends_at=session.ends_at if ends_at is None else ends_at,
        set_by=set_by,
        set_at=now,
    )
    return _bounded(wanted, now, max_instances, session_max_hours)


def stopped(current: Session | None, *, set_by: str, now: int, session_max_hours: int) -> Session | None:
    """The session to write when `set_by` stops the pool's session at the time `now`: the session
    with its end at `now`, or `None` when the pool has no session booked or active."""
    session = _booked_or_active(current, now, session_max_hours)
    if session is None:
        return None
    return dataclasses.replace(session, ends_at=now, set_by=set_by, set_at=now)
