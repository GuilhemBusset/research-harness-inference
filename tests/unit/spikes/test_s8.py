"""Spike S8's verdict (`spikes/s8_stickiness.py`): whether a student's requests stayed on one instance,
from recorded counts of what each instance served. No number here is a measurement; the instance
names are made up.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

import common
import s8_stickiness as s8
from rhi_common.keys import generate_key
from rhi_common.records import ScalerNote
from rhi_reporter.measure import Verdict, measurement_event

if TYPE_CHECKING:
    from collections.abc import Callable

Counts, Phase = s8.Counts, s8.Phase

STUCK = {1: Counts(10, {"instance-a": 10}), 2: Counts(10, {"instance-b": 10})}
REMAINED = {1: Counts(10, {"instance-a": 10}), 2: Counts(10, {"instance-a": 10})}


def test_each_student_on_one_instance_before_and_after_the_departure_passes() -> None:
    numbers = s8.numbers_of(2, {Phase.BEFORE: STUCK, Phase.AFTER: REMAINED})
    assert set(numbers) == {name.value for name in s8.Number}
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS
    # The numbers are the declared names, in the shape the reporter takes.
    names = [name.value for name in s8.Number]
    measurement_event("S8", "s8-stickiness", Verdict.PASS, numbers, names=names, commit="a" * 40, bus="bus")


def test_a_student_split_between_two_instances_before_the_departure_fails() -> None:
    split = {**STUCK, 2: Counts(10, {"instance-a": 3, "instance-b": 7})}
    numbers = s8.numbers_of(2, {Phase.BEFORE: split, Phase.AFTER: REMAINED})
    assert numbers[s8.most_served(2, Phase.BEFORE)] == 7
    assert s8.SPIKE.verdict(numbers) is Verdict.FAIL


def test_two_instances_in_one_zone_give_no_verdict() -> None:
    numbers = s8.numbers_of(1, {Phase.BEFORE: STUCK, Phase.AFTER: REMAINED})
    assert s8.SPIKE.verdict(numbers) is Verdict.NOT_MEASURED


def test_the_counts_are_the_rise_of_each_instance_s_count_in_the_window() -> None:
    start, end = 1000.0, 1300.0
    series = [
        common.Series({"instance": "instance-a", "zone": "zone-1"}, ((start, 40.0), (end, 52.0))),
        common.Series({"instance": "instance-b", "zone": "zone-2"}, ((start, 9.0), (end, 9.0))),
        # An instance whose series began inside the window counts from 0.
        common.Series({"instance": "instance-c", "zone": "zone-2"}, ((end, 3.0),)),
    ]
    assert s8.served_between(series, start, end) == {"instance-a": 12, "instance-c": 3}


def test_a_phase_is_measured_again_while_the_rebalance_number_changes() -> None:
    readings = iter([4, 4, 4, 5, 5, 5])
    measured: list[int] = []

    def measure() -> s8.PhaseCounts:
        measured.append(1)
        return STUCK

    # Unchanged on the first attempt: measured once.
    assert s8.measure_undisturbed(Phase.BEFORE, measure, lambda: next(readings), log=lambda _: None) == STUCK
    assert len(measured) == 1
    # Changed from 4 to 5 on the next: measured again, and kept once it holds.
    s8.measure_undisturbed(Phase.AFTER, measure, lambda: next(readings), log=lambda _: None)
    assert len(measured) == 3


def test_a_phase_disturbed_on_every_attempt_is_not_measured() -> None:
    numbers = iter(range(100))
    with pytest.raises(s8.Disturbed):
        s8.measure_undisturbed(Phase.BEFORE, lambda: STUCK, lambda: next(numbers), attempts=2, log=lambda _: None)
    # Counts that do not add up to what the student sent: another request was in its window.
    extra = {1: Counts(10, {"instance-a": 11}), 2: STUCK[2]}
    with pytest.raises(s8.Disturbed):
        s8.measure_undisturbed(Phase.BEFORE, lambda: extra, lambda: 0, attempts=2, log=lambda _: None)


def _answered(load: s8.loadgen.Load) -> list[s8.loadgen.Record]:
    """A record answered 200 for every request of the load's sessions."""
    records = []
    for number, session in enumerate(load.sessions):
        requests = session.requests()
        step = next(requests, None)
        while step is not None:
            records.append(s8.loadgen.Record(0, number, True, 200, 0.1, 0.1, 50.0, 200, 0, 20))
            try:
                step = requests.send("answer")
            except StopIteration:
                step = None
    return records


class _Pool:
    """A pool whose workspace answers are given in the order the job reads them."""

    name = "mock"

    def __init__(self, served: list[dict[str, int]] | None = None, observed: list[dict[str, str]] | None = None):
        self.windows, self.observed, self.seen = iter(served or []), observed or [], []

    def served(self, start: float, end: float) -> dict[str, int]:
        return next(self.windows)

    def serving(self) -> dict[str, str]:
        self.seen.append(self.observed[min(len(self.seen), len(self.observed) - 1)])
        return self.seen[-1]

    def present(self) -> set[str]:
        return set(self.seen[-1])

    def busy(self) -> int:
        return 0


def _replay(keys: list[str], kept: tuple[bool, bool], on_rest: Callable[[], None] = lambda: None) -> Callable:
    """A replay answering every request, in which a student's second request has its first's whole
    input cached, as the mock's cache reports it, when `kept` says it reached its first's instance."""

    async def replay(load: s8.loadgen.Load, supplied: list[str], endpoint: str, *, at_once: int) -> list:
        records = _answered(load)
        if isinstance(load.sessions[0], s8.Rest):
            on_rest()
            if kept[keys.index(supplied[0])]:
                records[0] = dataclasses.replace(records[0], cached_input_tokens=records[0].input_tokens)
        return records

    return replay


@pytest.mark.parametrize(
    ("second_student_on", "kept", "verdict"),
    [
        pytest.param("instance-a", (True, True), Verdict.PASS, id="instance-a-success"),
        # Student 2 moves to instance-b while both instances are in service.
        pytest.param("instance-b", (True, False), Verdict.FAIL, id="instance-b-failure"),
        # The two students swap instances: the window is the pass's, the cache tells them apart.
        pytest.param("instance-a", (False, False), Verdict.FAIL, id="swapped-failure"),
    ],
)
def test_the_first_requests_at_once_count_toward_the_verdict(
    monkeypatch: pytest.MonkeyPatch, second_student_on: str, kept: tuple[bool, bool], verdict: Verdict
) -> None:
    # The two first requests reach two instances; student 1's next requests reach instance-b.
    keys = [generate_key(), generate_key()]
    monkeypatch.setattr(s8.loadgen, "replay", _replay(keys, kept))
    monkeypatch.setattr(s8, "SETTLE_SECONDS", 0)
    firsts = {"instance-a": 1, "instance-b": 1}
    pool = _Pool([firsts, {"instance-b": 3}, {second_student_on: 3}])
    run = s8.Run(pool, "https://gateway.example.com", keys, 3, 8)  # ty: ignore[invalid-argument-type] - a test double
    before = s8.asyncio.run(run.phase(first_at_once=True))
    assert all(held.sent == 4 and held.adds_up() for held in before.values())
    assert s8.SPIKE.verdict(s8.numbers_of(2, {Phase.BEFORE: before, Phase.AFTER: REMAINED})) is verdict


def test_two_instances_are_two_seen_in_service_together(monkeypatch: pytest.MonkeyPatch) -> None:
    async def held(load: s8.loadgen.Load, keys: object, endpoint: str, *, at_once: int) -> list:
        (stream, *_) = load.sessions
        assert isinstance(stream, s8.HeldStream)
        await stream.stop.wait()
        return []

    monkeypatch.setattr(s8.loadgen, "replay", held)
    monkeypatch.setattr(s8, "POLL_SECONDS", 0)
    observed = [{"instance-a": "zone-1"}, {"instance-b": "zone-2"}, {"instance-a": "zone-1", "instance-b": "zone-2"}]
    pool = _Pool(observed=observed)
    run = s8.Run(pool, "https://gateway.example.com", [generate_key()] * 8, 3, 8)  # ty: ignore[invalid-argument-type] - a test double
    # Instance-a alone, then instance-b alone, is not two instances: the load is held until both are seen at once.
    assert s8.asyncio.run(run.two_instances()) == observed[2]
    assert len(pool.seen) == 3


class _Departing:
    """A pool of instance-a and instance-b from which instance-b leaves once `departed` is set."""

    name = "mock"

    def __init__(self, served: list[dict[str, int]]) -> None:
        self.windows, self.departed = iter(served), False

    def served(self, start: float, end: float) -> dict[str, int]:
        return next(self.windows)

    def present(self) -> set[str]:
        return {"instance-a"} if self.departed else {"instance-a", "instance-b"}

    def rebalance_number(self) -> int:
        return 0


def _run(monkeypatch: pytest.MonkeyPatch, pool: _Departing, *, depart_during_phase: bool = False) -> s8.Run:
    """A whole run on `pool`, both students' second requests finding their first's cache."""
    keys = [generate_key(), generate_key()]

    def on_rest() -> None:
        pool.departed = pool.departed or depart_during_phase

    async def two_instances(self: s8.Run) -> dict[str, str]:
        return {"instance-a": "zone-1", "instance-b": "zone-2"}

    async def wait(condition: Callable[[], bool], seconds: float, what: str) -> None:
        pool.departed = True
        assert condition()

    monkeypatch.setattr(s8.loadgen, "replay", _replay(keys, (True, True), on_rest))
    monkeypatch.setattr(s8.Run, "two_instances", two_instances)
    monkeypatch.setattr(s8, "_wait", wait)
    monkeypatch.setattr(s8, "SETTLE_SECONDS", 0)
    return s8.Run(pool, "https://gateway.example.com", keys, 2, 8)  # ty: ignore[invalid-argument-type] - a test double


def test_a_run_passes_once_a_student_has_moved_from_the_instance_that_left(monkeypatch: pytest.MonkeyPatch) -> None:
    # Student 1 is on instance-b, which leaves, and then reaches instance-a.
    after = [{"instance-a": 2}, {"instance-a": 2}]
    pool = _Departing([{"instance-a": 1, "instance-b": 1}, {"instance-b": 2}, {"instance-a": 2}, *after])
    assert s8.SPIKE.verdict(_run(monkeypatch, pool).measure()) is Verdict.PASS
    # Both students on one instance before the departure, the one that stayed (neither moved) or the
    # one that left (both moved): not one student moving while the other stays, so not measured.
    for on in ("instance-a", "instance-b"):
        pool = _Departing([{on: 2}, {on: 2}, {on: 2}, *after])
        with pytest.raises(s8.NotExercised):
            _run(monkeypatch, pool).measure()


def test_an_instance_leaving_during_the_phase_before_the_departure_is_not_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Instance-b leaves before student 1's next requests, which then reach instance-a.
    pool = _Departing([{"instance-a": 1, "instance-b": 1}, {"instance-a": 2}, {"instance-a": 2}])
    with pytest.raises(s8.Departed):
        _run(monkeypatch, pool, depart_during_phase=True).measure()
    # Student 1 moves to instance-a as instance-b leaves at the end of the phase, which the workspace
    # shows only when the job looks again: a departure, not a failure.
    pool = _Departing([{"instance-a": 1, "instance-b": 1}, {"instance-b": 1, "instance-a": 1}, {"instance-a": 2}])
    run = _run(monkeypatch, pool)
    monkeypatch.setattr(s8.time, "sleep", lambda seconds: setattr(pool, "departed", True))
    with pytest.raises(s8.Departed):
        run.measure()


class _Table:
    """The pool-state table, whose note asks for `asked` instances. An eventually consistent read
    still sees the note before the scaler's last write."""

    def __init__(self, asked: int, number: int) -> None:
        self.asked, self.number = asked, number

    def get_item(self, *, Key: dict, ConsistentRead: bool = False) -> dict:
        number = self.number if ConsistentRead else 0
        return {"Item": ScalerNote(Key["pool"], self.asked, None, 1000, number).to_item()}


class _Workspace:
    """A workspace in which instance-a and instance-b both wrote a sample just now."""

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        return [common.Series({"instance": one}, ((end, end),)) for one in ("instance-a", "instance-b")]


def test_the_pool_reads_the_scaler_s_last_write() -> None:
    pool = s8.Pool("mock", _Workspace(), _Table(2, 5))  # ty: ignore[invalid-argument-type] - a test double
    assert pool.rebalance_number() == 5
    assert pool.present() == {"instance-a", "instance-b"}
    # The scaler asks for one instance while two still write: one is leaving.
    with pytest.raises(s8.Leaving):
        s8.Pool("mock", _Workspace(), _Table(1, 5)).present()  # ty: ignore[invalid-argument-type] - a test double


def test_a_student_that_moves_and_returns_keeps_its_split() -> None:
    # Student 1 stays on instance-a; student 2 opens on instance-b, misses its cache on instance-a,
    # and goes back to instance-b: its first is the one left, on instance-b.
    window = {1: Counts(2, {"instance-a": 2}), 2: Counts(2, {"instance-a": 1, "instance-b": 1})}
    counts = s8.with_openings({"instance-a": 1, "instance-b": 1}, window, {1: True, 2: False})
    assert counts[2] == Counts(3, {"instance-a": 1, "instance-b": 2})
    assert all(held.adds_up() for held in counts.values())
    assert s8.SPIKE.verdict(s8.numbers_of(2, {Phase.BEFORE: counts, Phase.AFTER: REMAINED})) is Verdict.FAIL


def test_each_run_talks_in_conversations_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same canary students in a later run: an instance's cache of the earlier run's
    # conversation cannot be taken for this run's first request.
    bodies: list[bytes] = []

    async def replay(load: s8.loadgen.Load, supplied: list[str], endpoint: str, *, at_once: int) -> list:
        firsts = [session for session in load.sessions if isinstance(session, s8.Opening)]
        bodies.extend(next(session.requests())[1].body for session in firsts)
        return _answered(load)

    monkeypatch.setattr(s8.loadgen, "replay", replay)
    monkeypatch.setattr(s8, "SETTLE_SECONDS", 0)
    keys = [generate_key(), generate_key()]
    for _ in range(2):
        pool = _Pool([{"instance-a": 2}, {"instance-a": 1}, {"instance-a": 1}])
        run = s8.Run(pool, "https://gateway.example.com", keys, 1, 8)  # ty: ignore[invalid-argument-type] - a test double
        s8.asyncio.run(run.phase(first_at_once=True))
    assert len(bodies) == 4
    assert bodies[0] != bodies[2]


class _Clock:
    """The time of the job, which moves only as it sleeps."""

    def __init__(self, now: float) -> None:
        self.now = now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Scaling:
    """A workspace and a pool-state table over instance-a and instance-b: instance-b writes until
    `stops_writing`, and the scaler asks for one instance from `scales_in`."""

    def __init__(self, clock: _Clock, *, stops_writing: float, scales_in: float) -> None:
        self.clock, self.stops_writing, self.scales_in = clock, stops_writing, scales_in

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        latest = {"instance-a": end, "instance-b": min(end, self.stops_writing)}
        return [common.Series({"instance": one}, ((end, at),)) for one, at in latest.items()]

    def get_item(self, *, Key: dict, ConsistentRead: bool = False) -> dict:
        asked = 1 if self.clock.now >= self.scales_in else 2
        return {"Item": ScalerNote(Key["pool"], asked, None, int(self.clock.now)).to_item()}


def _scaling_run(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, scaling: _Scaling, before: s8.PhaseCounts, after: list[float]
) -> s8.Run:
    """A run on the real pool reader, whose phases give `before` and then each student on instance-a;
    it records in `after` the time each later phase began."""

    async def two_instances(self: s8.Run) -> dict[str, str]:
        return {"instance-a": "zone-1", "instance-b": "zone-2"}

    async def phase(self: s8.Run, *, first_at_once: bool) -> s8.PhaseCounts:
        if first_at_once:
            return before
        after.append(clock.now)
        return {student: Counts(2, {"instance-a": 2}) for student in s8.STUDENTS}

    async def wait(condition: Callable[[], bool], seconds: float, what: str) -> None:
        while not condition():
            clock.sleep(s8.POLL_SECONDS)

    monkeypatch.setattr(s8.time, "time", lambda: clock.now)
    monkeypatch.setattr(s8.time, "sleep", clock.sleep)
    monkeypatch.setattr(s8.Run, "two_instances", two_instances)
    monkeypatch.setattr(s8.Run, "phase", phase)
    monkeypatch.setattr(s8, "_wait", wait)
    pool = s8.Pool("mock", scaling, scaling)  # ty: ignore[invalid-argument-type] - a test double
    return s8.Run(pool, "https://gateway.example.com", [generate_key(), generate_key()], 2, 8)


def test_a_stale_sample_is_not_the_departure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Instance-b stops writing at 1000 while the scaler asks for two until 2000: the phase after
    # the departure waits for the scaler.
    clock, after = _Clock(1000), []
    scaling = _Scaling(clock, stops_writing=1000, scales_in=2000)
    stuck = {1: Counts(3, {"instance-a": 3}), 2: Counts(3, {"instance-b": 3})}
    numbers = _scaling_run(monkeypatch, clock, scaling, stuck, after).measure()
    assert after and after[0] >= 2000
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS


def test_a_scale_in_after_the_phase_keeps_its_split(monkeypatch: pytest.MonkeyPatch) -> None:
    # Student 1 was split while both instances were in service; the scaler asks for one instance a
    # minute after the phase ended.
    clock, after = _Clock(1000), []
    scaling = _Scaling(clock, stops_writing=1059, scales_in=1060)
    split = {1: Counts(3, {"instance-a": 1, "instance-b": 2}), 2: Counts(3, {"instance-a": 3})}
    numbers = _scaling_run(monkeypatch, clock, scaling, split, after).measure()
    assert after and after[0] >= 1060
    assert s8.SPIKE.verdict(numbers) is Verdict.FAIL
    # Instance-b stopping writing while the scaler still asks for both: a departure, not a failure.
    clock = _Clock(1000)
    scaling = _Scaling(clock, stops_writing=1000, scales_in=2000)
    with pytest.raises(s8.Departed):
        _scaling_run(monkeypatch, clock, scaling, split, []).measure()
