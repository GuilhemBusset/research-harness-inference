"""How long a pool has been in service outside its own session (`rhi_scaler.outside_sessions`)."""

from __future__ import annotations

from collections.abc import Sequence

from rhi_common.pool_config import parse_pools
from rhi_common.records import Session
from rhi_scaler.decide import decide
from rhi_scaler.model import Inputs, Memory, PoolInputs, Supply
from rhi_scaler.outside_sessions import carry, minutes, stretch_since

T0 = 1_800_000_000
HOUR = 3600
ENTRY = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "prices": {"t4g.small": 0.0168},
    "max_instances": 2,
    "warm_instances": 0,
    "wake": "on-demand",
    "target_concurrency": 4,
    "context_length": 32_768,
    "largest_output": 4_096,
}
COURSEWORK, MOCK = parse_pools({"coursework": ENTRY, "mock": ENTRY})


def session(starts_at: int, ends_at: int, pool: str = "coursework") -> Session:
    return Session(pool, 1, starts_at, ends_at, set_by="instructor", set_at=starts_at - HOUR)


def counts(runs: Sequence[tuple[int, int, Session | None]], session_max_hours: int = 12) -> list[int]:
    """The count after each run, a run being its time, its instances in service and the pool's session."""
    since: int | None = None
    out = []
    for now, in_service, record in runs:
        since = stretch_since(since, in_service, record, now, session_max_hours=session_max_hours)
        out.append(minutes(since, now))
    return out


def test_an_hour_and_a_minute_in_service_outside_a_session_counts_61_minutes() -> None:
    # A pool with no session, one whose session ended before, and one booked for later.
    for record in (None, session(T0 - 2 * HOUR, T0 - HOUR), session(T0 + 2 * HOUR, T0 + 3 * HOUR)):
        assert counts([(T0 + 60 * n, 1, record) for n in range(62)])[-1] == 61


def test_minutes_while_the_pool_s_session_is_active_count_nothing() -> None:
    active = session(T0 - HOUR, T0 + 2 * HOUR)
    assert set(counts([(T0 + 60 * n, 2, active) for n in range(90)])) == {0}


def test_the_count_restarts_after_the_pool_went_to_zero_and_after_a_session() -> None:
    after_zero = [(T0 + 60 * n, 1, None) for n in range(31)]
    after_zero += [(T0 + 1860, 0, None)]
    after_zero += [(T0 + 1920 + 60 * n, 1, None) for n in range(11)]
    assert counts(after_zero)[30:] == [30, 0, *range(11)]

    # A session that starts at the 31st minute and lasts until the 32nd: one run sees it active.
    brief = session(T0 + 1860, T0 + 1920)
    assert counts([(T0 + 60 * n, 1, brief) for n in range(43)])[30:] == [30, 0, *range(11)]


def test_a_stretch_starts_at_the_end_of_the_pool_s_session_not_before_it() -> None:
    # In service before its session, then runs missed while the session came and went: the next
    # run, a minute after the session's end, counts from that end and not from the first run.
    came_and_went = session(T0 + 60, T0 + HOUR)
    assert counts([(T0, 1, came_and_went), (T0 + HOUR + 60, 1, came_and_went), (T0 + HOUR + 120, 1, came_and_went)]) == [0, 1, 2]
    # The end is the one every reader takes: a record that gives a later end than the longest
    # session allows ended there.
    long = Session("coursework", 1, T0 + 60, T0 + 9 * HOUR, set_by="instructor", set_at=T0)
    assert counts([(T0, 1, long), (T0 + HOUR + 120, 1, long)], session_max_hours=1) == [0, 1]


def test_a_session_active_on_another_pool_does_not_end_a_pool_s_stretch() -> None:
    names = ("instance-a",)
    in_service = Supply(desired=1, in_service=names, running=names)
    since = Memory(outside_sessions_since=T0 - 30 * 60)
    inputs = Inputs(
        now=T0,
        pools=(
            # Coursework: in service for half an hour, and its own session ended before that.
            PoolInputs(COURSEWORK, in_service, memory=since, session=session(T0 - 3 * HOUR, T0 - HOUR)),
            # Mock: in service too, and its session is active.
            PoolInputs(MOCK, in_service, memory=since, session=session(T0 - HOUR, T0 + HOUR, "mock")),
        ),
    )
    carried = carry(inputs, decide(inputs))
    assert [(pool.pool, pool.memory.outside_sessions_since) for pool in carried.pools] == [
        ("coursework", T0 - 30 * 60),
        ("mock", None),
    ]
