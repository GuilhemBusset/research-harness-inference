"""A pool's session under its bounds (`rhi_common.sessions`): when it is active, what it asks for,
and what a booking, a change and a stop write or refuse."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from rhi_common.records import Session
from rhi_common.sessions import SessionError, changed, is_active, requested, started, stopped

NOW = 1_800_000_000
HOUR = 3600
# The two bounds of the configuration: the pool's `max_instances` and `session_max_hours`.
MOST = 4
HOURS = 12

# A session written an hour ago that starts now, one booked for later, and one whose end has come.
ACTIVE = Session(
    pool="coursework", instances=2, starts_at=NOW, ends_at=NOW + 4 * HOUR, set_by="instructor", set_at=NOW - HOUR
)
BOOKED = dataclasses.replace(ACTIVE, starts_at=NOW + 2 * HOUR, ends_at=NOW + 6 * HOUR)
ENDED = dataclasses.replace(ACTIVE, starts_at=NOW - 5 * HOUR, ends_at=NOW, set_at=NOW - 6 * HOUR)


def active(session: Session | None, now: int) -> bool:
    return is_active(session, now, session_max_hours=HOURS)


def asked(session: Session | None, now: int) -> int:
    return requested(session, now, max_instances=MOST, session_max_hours=HOURS)


def start(current: Session | None, **given: int) -> Session:
    """A booking at the time NOW of two instances for four hours from now, but for what is given."""
    values = {"instances": 2, "starts_at": NOW, "ends_at": NOW + 4 * HOUR} | given
    return started(
        current, pool="coursework", set_by="assistant", now=NOW, max_instances=MOST, session_max_hours=HOURS, **values
    )


def change(current: Session | None, **given: int) -> Session:
    return changed(current, set_by="assistant", now=NOW, max_instances=MOST, session_max_hours=HOURS, **given)


def stop(current: Session | None) -> Session | None:
    return stopped(current, set_by="assistant", now=NOW, session_max_hours=HOURS)


def refuses(call: Callable[[], Session]) -> bool:
    try:
        call()
    except SessionError:
        return True
    return False


def test_a_session_is_active_at_its_start_and_not_at_its_end() -> None:
    assert not active(ACTIVE, ACTIVE.starts_at - 1)
    assert active(ACTIVE, ACTIVE.starts_at)
    assert active(ACTIVE, ACTIVE.ends_at - 1)
    assert not active(ACTIVE, ACTIVE.ends_at)
    assert not active(None, NOW)


@pytest.mark.parametrize("set_at", [NOW - HOUR, NOW + 3 * HOUR], ids=["written before its start", "written after"])
def test_a_session_ends_12_hours_after_the_later_of_its_start_and_its_last_write(set_at: int) -> None:
    later = max(NOW, set_at)
    long = dataclasses.replace(ACTIVE, ends_at=later + 20 * HOUR, set_at=set_at)
    assert active(long, later)
    assert active(long, later + 12 * HOUR - 1)
    assert not active(long, later + 12 * HOUR)
    assert asked(long, later + 12 * HOUR) == 0
    # The bound is the configuration's: with another, the session ends elsewhere.
    assert is_active(long, later + 12 * HOUR, session_max_hours=20)
    assert not is_active(long, later + 6 * HOUR, session_max_hours=6)


def test_requested_is_the_sessions_instances_while_it_is_active_and_never_above_max_instances() -> None:
    assert asked(ACTIVE, NOW) == 2
    assert asked(ACTIVE, NOW - 1) == 0
    assert asked(ACTIVE, ACTIVE.ends_at) == 0
    assert asked(BOOKED, NOW) == 0
    assert asked(None, NOW) == 0
    # A record written by hand may ask for more than the pool may hold.
    assert asked(dataclasses.replace(ACTIVE, instances=MOST + 5), NOW) == MOST
    for instances in range(MOST + 6):
        for now in range(NOW - HOUR, NOW + 5 * HOUR, HOUR // 2):
            assert asked(dataclasses.replace(ACTIVE, instances=instances), now) <= MOST


def test_a_booking_is_the_session_to_write() -> None:
    # The most instances, from now, for the longest a session may last.
    booking = start(None, instances=MOST, ends_at=NOW + HOURS * HOUR)
    assert booking == Session(
        pool="coursework",
        instances=MOST,
        starts_at=NOW,
        ends_at=NOW + HOURS * HOUR,
        set_by="assistant",
        set_at=NOW,
    )
    assert start(None, starts_at=NOW + HOUR, ends_at=NOW + 2 * HOUR).starts_at == NOW + HOUR
    # A pool whose session has ended can be booked again: at its end, or at the bound for a record
    # that gives a later one.
    assert start(ENDED) == start(None)
    forgotten = dataclasses.replace(ENDED, starts_at=NOW - HOURS * HOUR, ends_at=NOW + 8 * HOUR, set_at=NOW - 13 * HOUR)
    assert start(forgotten) == start(None)


def test_a_change_replaces_what_it_is_given_and_keeps_the_rest() -> None:
    rewritten = dataclasses.replace(BOOKED, set_by="assistant", set_at=NOW)
    assert change(BOOKED, instances=3) == dataclasses.replace(rewritten, instances=3)
    assert change(BOOKED, starts_at=NOW + HOUR) == dataclasses.replace(rewritten, starts_at=NOW + HOUR)
    assert change(BOOKED, ends_at=NOW + 5 * HOUR) == dataclasses.replace(rewritten, ends_at=NOW + 5 * HOUR)
    # A start has passed once now is after it: at that very second it can still be moved.
    assert change(ACTIVE, starts_at=NOW + HOUR).starts_at == NOW + HOUR
    # A session that started 10 hours ago may be kept until the bound after now, and the start it
    # already has is not a start moved.
    old = dataclasses.replace(ACTIVE, starts_at=NOW - 10 * HOUR, ends_at=NOW + HOUR, set_at=NOW - 11 * HOUR)
    assert change(old, ends_at=NOW + HOURS * HOUR).ends_at == NOW + HOURS * HOUR
    assert change(old, starts_at=old.starts_at, instances=1).instances == 1


@pytest.mark.parametrize("current", [ACTIVE, BOOKED], ids=["active", "booked"])
def test_a_stop_ends_the_session_now(current: Session) -> None:
    ended = stop(current)
    assert ended == dataclasses.replace(current, ends_at=NOW, set_by="assistant", set_at=NOW)
    assert not active(ended, NOW)
    assert all(asked(ended, now) == 0 for now in (NOW, current.starts_at, current.ends_at - 1))
    # The pool has no session booked or active any more: it can be booked, and there is nothing to stop.
    assert start(ended) == start(None)
    assert stop(ended) is None


def test_each_refusal() -> None:
    passed = dataclasses.replace(ACTIVE, starts_at=NOW - 1)
    refused: dict[str, Callable[[], Session]] = {
        # `started`: what is accepted is in `test_a_booking_is_the_session_to_write`.
        "a booking of a pool whose session is booked": lambda: start(BOOKED),
        "a booking of a pool whose session is active": lambda: start(ACTIVE),
        "a booking of no instance": lambda: start(None, instances=0),
        "a booking of more than max_instances": lambda: start(None, instances=MOST + 1),
        "a booking that starts before now": lambda: start(None, starts_at=NOW - 1),
        "a booking that ends at its start": lambda: start(None, starts_at=NOW + HOUR, ends_at=NOW + HOUR),
        "a booking that ends before its start": lambda: start(None, starts_at=NOW + HOUR, ends_at=NOW + HOUR - 1),
        "a booking longer than session_max_hours": lambda: start(None, ends_at=NOW + HOURS * HOUR + 1),
        "a booking ahead longer than session_max_hours": lambda: start(
            None, starts_at=NOW + HOUR, ends_at=NOW + (HOURS + 1) * HOUR + 1
        ),
        # `changed`: what is accepted is in `test_a_change_replaces_what_it_is_given_and_keeps_the_rest`.
        "a change of a pool with no session": lambda: change(None, instances=1),
        "a change of a session that has ended": lambda: change(ENDED, instances=1, ends_at=NOW + HOUR),
        "a start moved later once it has passed": lambda: change(passed, starts_at=NOW + HOUR),
        "a start moved earlier once it has passed": lambda: change(passed, starts_at=NOW - HOUR),
        "a change to no instance": lambda: change(ACTIVE, instances=0),
        "a change to more than max_instances": lambda: change(ACTIVE, instances=MOST + 1),
        "an end moved to now": lambda: change(ACTIVE, ends_at=NOW),
        "an end moved to the start of a booked session": lambda: change(BOOKED, ends_at=BOOKED.starts_at),
        "a start moved to the end": lambda: change(BOOKED, starts_at=BOOKED.ends_at),
        "an end more than session_max_hours after now": lambda: change(passed, ends_at=NOW + HOURS * HOUR + 1),
        "an end more than session_max_hours after a start to come": lambda: change(
            BOOKED, ends_at=BOOKED.starts_at + HOURS * HOUR + 1
        ),
    }
    assert [what for what, call in refused.items() if not refuses(call)] == []
    # Each bound is exact: one second inside it, the same booking and the same change are accepted.
    assert start(None, instances=1).instances == 1
    assert start(None, ends_at=NOW + HOURS * HOUR).ends_at == NOW + HOURS * HOUR
    assert change(passed, ends_at=NOW + HOURS * HOUR).ends_at == NOW + HOURS * HOUR
    assert change(BOOKED, ends_at=BOOKED.starts_at + HOURS * HOUR).ends_at == BOOKED.starts_at + HOURS * HOUR

    # There is nothing to stop in a pool with no session booked or active.
    assert stop(None) is None
    assert stop(ENDED) is None
