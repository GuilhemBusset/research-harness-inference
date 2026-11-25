"""The scaler's remote-write series (`rhi_scaler.metrics`)."""

from __future__ import annotations

import pytest

from rhi_common.pool_config import parse_pools
from rhi_common.records import LaunchFailure, Mode, ScalerNote, Session
from rhi_scaler.metrics import DECISIONS, LAUNCH_FAILURE, SUPPLY_GAUGES, DecisionCounts, decode, encode, fleet_series
from rhi_scaler.model import Decision, Inputs, Memory, PoolDecision, PoolInputs, Reason, Supply

NOW = 1_800_000_000
HOUR = 3600
POOL = parse_pools(
    {
        "mock": {
            "engine": "mock",
            "instance_types": ["t4g.small"],
            "prices": {"t4g.small": 0.0168},
            "max_instances": 2,
            "warm_instances": 1,
            "wake": "on-demand",
            "target_concurrency": 4,
            "context_length": 32_768,
            "largest_output": 4_096,
        }
    }
)[0]
# A pool where nothing waits, and one that waits for one of the three instances asked of AWS.
QUIET = ScalerNote(pool="mock", capacity_asked=1, no_capacity_at=None, written_at=NOW, in_service=1)
SHORT = ScalerNote(
    pool="mock",
    capacity_asked=3,
    no_capacity_at=None,
    written_at=NOW,
    requested=4,
    starting=1,
    in_service=1,
    draining=2,
    waiting=1,
    waiting_since=NOW - 600,
)
WINDOW = ("rhi_scaler_session_starts_at_seconds", "rhi_scaler_session_ends_at_seconds")


def run(
    now: int,
    reason: Reason,
    counts: DecisionCounts,
    note: ScalerNote = QUIET,
    session: Session | None = None,
) -> dict[tuple[str, ...], list[tuple[float, int]]]:
    """The series of one run, by their label values in the order of the label names: the series'
    name, then the cause if it has one, the pool, and the reason if it has one."""
    pool = PoolInputs(config=POOL, mode=Mode.ENABLED, supply=Supply(desired=1, in_service=("i-a",)), session=session)
    inputs = Inputs(now=now, pools=(pool,))
    decision = Decision(pools=(PoolDecision("mock", 1, (), False, Memory(), reason),))
    out = {}
    for timeseries in decode(encode(fleet_series(inputs, decision, counts, notes={"mock": note}))).timeseries:
        names = [label.name for label in timeseries.labels]
        assert names == sorted(names) and names[0] == "__name__"
        key = tuple(label.value for label in timeseries.labels)
        out[key] = [(sample.value, sample.timestamp) for sample in timeseries.samples]
    return out


def last(now: int, note: ScalerNote = QUIET, session: Session | None = None) -> dict[tuple[str, ...], float]:
    """The value each series holds after one run at `now`."""
    return {key: samples[-1][0] for key, samples in run(now, Reason.UNCHANGED, DecisionCounts(), note, session).items()}


def test_the_decisions_counter_counts_from_zero_and_keeps_counting() -> None:
    counts = DecisionCounts()
    first = run(NOW, Reason.WAKE, counts)
    # A series the process sends for the first time starts at zero, so the first decision counts.
    assert first[(DECISIONS, "mock", "wake")] == [(0.0, NOW * 1000 - 1), (1.0, NOW * 1000)]
    second = run(NOW + 60, Reason.UNCHANGED, counts)
    third = run(NOW + 120, Reason.UNCHANGED, counts)
    assert second[(DECISIONS, "mock", "wake")] == [(1.0, (NOW + 60) * 1000)]
    assert third[(DECISIONS, "mock", "unchanged")] == [(2.0, (NOW + 120) * 1000)]
    assert third[("rhi_scaler_instances_in_service", "mock")] == [(1.0, (NOW + 120) * 1000)]


def test_what_the_pool_was_asked_and_what_waits_is_sent_with_the_pool_s_label() -> None:
    session = Session("mock", 4, NOW - HOUR, NOW + HOUR, set_by="instructor", set_at=NOW - 2 * HOUR)
    sent = last(NOW, SHORT, session)
    # Each series under the pool's label alone, with what the note and the session give.
    assert {name: sent[(name, "mock")] for name in (*SUPPLY_GAUGES, *WINDOW)} == {
        "rhi_scaler_session_active": 1.0,
        "rhi_scaler_instances_requested": 4.0,
        "rhi_scaler_instances_waiting": 1.0,
        "rhi_scaler_waiting_seconds": 600.0,
        "rhi_scaler_instances_starting": 1.0,
        "rhi_scaler_instances_draining": 2.0,
        "rhi_scaler_session_starts_at_seconds": float(NOW - HOUR),
        "rhi_scaler_session_ends_at_seconds": float(NOW + HOUR),
    }
    # A pool with no session, where nothing waits, sends the six all the same, at zero.
    assert {name: last(NOW)[(name, "mock")] for name in SUPPLY_GAUGES} == dict.fromkeys(SUPPLY_GAUGES, 0.0)


def test_the_session_s_window_is_sent_while_the_session_has_not_ended() -> None:
    def window(session: Session | None) -> list[float] | None:
        sent = last(NOW, session=session)
        assert sum(name in WINDOW for name, *_ in sent) in (0, 2)
        return [sent[(name, "mock")] for name in (*WINDOW, "rhi_scaler_session_active")] if (WINDOW[0], "mock") in sent else None

    def session(starts_at: int, ends_at: int, set_at: int = NOW - 2 * HOUR) -> Session:
        return Session("mock", 2, starts_at, ends_at, set_by="instructor", set_at=set_at)

    # Booked and not yet active: the window is sent, and the session is not active.
    assert window(session(NOW + HOUR, NOW + 3 * HOUR)) == [NOW + HOUR, NOW + 3 * HOUR, 0.0]
    assert window(session(NOW - HOUR, NOW + 1)) == [NOW - HOUR, NOW + 1, 1.0]
    # Ended, at the second of its end, and a pool with no session: no window.
    assert window(session(NOW - HOUR, NOW)) is None
    assert window(None) is None
    # A record that gives a later end than a session may have is sent as ending where it is read
    # to end, 12 hours after its start, and is over from then on.
    assert window(session(NOW - 2 * HOUR, NOW + 20 * HOUR)) == [NOW - 2 * HOUR, NOW + 10 * HOUR, 1.0]
    assert window(session(NOW - 12 * HOUR, NOW + 20 * HOUR, set_at=NOW - 13 * HOUR)) is None


@pytest.mark.parametrize("cause", [None, *LaunchFailure])
def test_the_cause_has_its_four_series_and_one_of_them_at_most_reads_1(cause: LaunchFailure | None) -> None:
    waiting = ScalerNote(
        pool="mock", capacity_asked=2, no_capacity_at=None, written_at=NOW, waiting=2, waiting_since=NOW - 60, launch_failure=cause
    )
    sent = last(NOW, waiting)
    causes = {key[1:]: value for key, value in sent.items() if key[0] == LAUNCH_FAILURE}
    # One series for each of the four words, each with the cause and the pool, on every run.
    words = ("insufficient-capacity", "quota", "launches-suspended", "launch-failed")
    assert causes == {(word, "mock"): 1.0 if cause is not None and word == cause.value else 0.0 for word in words}
    assert sum(causes.values()) == (0.0 if cause is None else 1.0)
