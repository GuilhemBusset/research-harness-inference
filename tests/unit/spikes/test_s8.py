"""Spike S8's verdict (`spikes/s8_stickiness.py`): whether a student's requests stayed on one instance,
from recorded counts of what each instance served; and the session the job books on the pool, lowers
for the departure and stops. No number here is a measurement; the instance names are made up.

moto holds the pool-state table in process: no account, no credentials, no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import itertools
import urllib.request
from contextlib import ExitStack
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import boto3
import httpx
import pytest
from moto import mock_aws
from prometheus_client.parser import text_string_to_metric_families
from starlette.testclient import TestClient

import common
import s8_stickiness as s8
from rhi import config
from rhi_common import sessions
from rhi_common.keys import generate_key
from rhi_common.records import SESSION_RECORD, ScalerNote, Session
from rhi_common.tables import POOL_STATE
from rhi_lifecycle.launch import Engine
from rhi_mock_engine.app import Settings, create_app
from rhi_reporter.measure import Verdict, measurement_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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
        # Before the load's first look, the instances the session brought: the last observed.
        return set(self.seen[-1] if self.seen else self.observed[-1])

    def noted(self) -> tuple[int, int]:
        return (2, 2)

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
    # Instance-a alone, then instance-b alone, is not two instances serving: the load is held until
    # both are seen at once.
    assert s8.asyncio.run(run.two_instances()) == observed[2]
    assert len(pool.seen) == 3


class _Clock:
    """The time of the job, which moves only as it sleeps, with each sleep it took."""

    def __init__(self, now: float) -> None:
        self.now = now
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _Departing:
    """A pool of instance-a and instance-b from which instance-b leaves once `departed` is set."""

    name = "mock"

    def __init__(self, served: list[dict[str, int]]) -> None:
        self.windows, self.departed = iter(served), False

    def served(self, start: float, end: float) -> dict[str, int]:
        return next(self.windows)

    def present(self) -> set[str]:
        return {"instance-a"} if self.departed else {"instance-a", "instance-b"}

    def noted(self) -> tuple[int, int]:
        return (1, 1) if self.departed else (2, 2)

    def rebalance_number(self) -> int:
        return 0

    def depart(self) -> None:
        self.departed = True


class _Session:
    """The job's session, with each number it was lowered to: `on_lower` is what the scaler then does."""

    def __init__(self, on_lower: Callable[[], None] = lambda: None) -> None:
        self.lowered: list[int] = []
        self.on_lower = on_lower

    def lower(self, instances: int) -> None:
        self.lowered.append(instances)
        self.on_lower()


def _run(
    monkeypatch: pytest.MonkeyPatch, pool: _Departing, clock: _Clock, *, depart_during_phase: bool = False
) -> s8.Run:
    """A whole run on `pool`, both students' second requests finding their first's cache, on the
    time of `clock`. Instance-b leaves when the run's session is lowered (`_Session(pool.depart)`)."""
    keys = [generate_key(), generate_key()]

    def on_rest() -> None:
        pool.departed = pool.departed or depart_during_phase

    async def two_instances(self: s8.Run) -> dict[str, str]:
        return {"instance-a": "zone-1", "instance-b": "zone-2"}

    async def wait(condition: Callable[[], bool], seconds: float, what: str) -> None:
        assert condition()

    monkeypatch.setattr(s8.loadgen, "replay", _replay(keys, (True, True), on_rest))
    monkeypatch.setattr(s8.Run, "two_instances", two_instances)
    monkeypatch.setattr(s8, "_wait", wait)
    monkeypatch.setattr(s8, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(s8.time, "time", lambda: clock.now)
    monkeypatch.setattr(s8.time, "sleep", clock.sleep)
    return s8.Run(pool, "https://gateway.example.com", keys, 2, 8)  # ty: ignore[invalid-argument-type] - a test double


def test_a_run_passes_once_a_student_has_moved_from_the_instance_that_left(monkeypatch: pytest.MonkeyPatch) -> None:
    # Student 1 is on instance-b, which leaves, and then reaches instance-a.
    after = [{"instance-a": 2}, {"instance-a": 2}]
    pool = _Departing([{"instance-a": 1, "instance-b": 1}, {"instance-b": 2}, {"instance-a": 2}, *after])
    clock, session = _Clock(1000), _Session(pool.depart)
    assert not pool.departed
    numbers = _run(monkeypatch, pool, clock).measure(session)  # ty: ignore[invalid-argument-type] - a test double
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS
    # The departure is the job's doing: it lowered its session to one instance, once.
    assert session.lowered == [1]
    # The first phase sent once the gateway held the number read at 1000; the unchanged number needed no other wait.
    assert clock.slept == [s8.GATEWAY_SECONDS]
    assert clock.now == 1000 + s8.GATEWAY_SECONDS
    # Both students on one instance before the departure, the one that stayed (neither moved) or the
    # one that left (both moved): not one student moving while the other stays, so not measured.
    for on in ("instance-a", "instance-b"):
        pool = _Departing([{on: 2}, {on: 2}, {on: 2}, *after])
        with pytest.raises(s8.NotExercised):
            _run(monkeypatch, pool, _Clock(1000)).measure(_Session(pool.depart))  # ty: ignore[invalid-argument-type] - a test double


def test_an_instance_leaving_during_the_phase_before_the_departure_is_not_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Instance-b leaves before student 1's next requests, which then reach instance-a.
    pool, clock = _Departing([{"instance-a": 1, "instance-b": 1}, {"instance-a": 2}, {"instance-a": 2}]), _Clock(1000)
    session = _Session(pool.depart)
    with pytest.raises(s8.Departed):
        _run(monkeypatch, pool, clock, depart_during_phase=True).measure(session)  # ty: ignore[invalid-argument-type] - a test double
    # The run ended before the job lowered its session.
    assert clock.slept == [s8.GATEWAY_SECONDS] and session.lowered == []
    # Student 1 moves to instance-a as instance-b leaves at the end of the phase, which the workspace
    # shows only when the job looks again: a departure, not a failure.
    pool = _Departing([{"instance-a": 1, "instance-b": 1}, {"instance-b": 1, "instance-a": 1}, {"instance-a": 2}])
    clock, in_service = _Clock(1000), []
    run = _run(monkeypatch, pool, clock)

    def departing(seconds: float) -> None:
        in_service.append(not pool.departed)
        clock.sleep(seconds)
        pool.departed = True

    monkeypatch.setattr(s8.time, "sleep", departing)
    with pytest.raises(s8.Departed):
        run.measure(_Session())  # ty: ignore[invalid-argument-type] - a test double
    # Instance-b was in service when the gateway's propagation wait began, and left during it.
    assert in_service == [True]
    assert clock.slept == [s8.GATEWAY_SECONDS]
    assert pool.departed


class _Table:
    """The pool-state table, whose note asks for `asked` instances. An eventually consistent read
    still sees the note before the scaler's last write."""

    def __init__(self, asked: int, number: int) -> None:
        self.asked, self.number = asked, number

    def get_item(self, *, Key: dict, ConsistentRead: bool = False) -> dict:
        number = self.number if ConsistentRead else 0
        return {"Item": ScalerNote(Key["pool"], self.asked, None, 1000, number).to_item()}


def _scaler_wrote(query: str, end: float, in_service: int) -> tuple[tuple[float, float], ...]:
    """The scaler's gauge of the instances in service, written at `end`: its value, or when it was
    written for a query of its `timestamp()`."""
    return ((end, end),) if query.startswith("timestamp(") else ((end, in_service),)


class _Workspace:
    """A workspace in which instance-a and instance-b both wrote a sample just now, and the scaler
    read both in service."""

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s8.INSTANCES_IN_SERVICE in query:
            return [common.Series({"pool": "mock"}, _scaler_wrote(query, end, 2))]
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


class _Scaling:
    """A workspace and a pool-state table over instance-a and instance-b: instance-b writes until
    `stops_writing`, and the scaler asks for one instance, and reads one in service, from `scales_in`.
    What it reads in service is in its gauge and in its note."""

    def __init__(self, clock: _Clock, *, stops_writing: float, scales_in: float) -> None:
        self.clock, self.stops_writing, self.scales_in = clock, stops_writing, scales_in

    def in_service(self) -> int:
        return 1 if self.clock.now >= self.scales_in else 2

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s8.INSTANCES_IN_SERVICE in query:
            return [common.Series({"pool": "mock"}, _scaler_wrote(query, end, self.in_service()))]
        latest = {"instance-a": end, "instance-b": min(end, self.stops_writing)}
        return [common.Series({"instance": one}, ((end, at),)) for one, at in latest.items()]

    def get_item(self, *, Key: dict, ConsistentRead: bool = False) -> dict:
        asked = 1 if self.clock.now >= self.scales_in else 2
        note = ScalerNote(Key["pool"], asked, None, int(self.clock.now), in_service=self.in_service())
        return {"Item": note.to_item()}


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


def _measured(run: s8.Run) -> dict[str, int | float]:
    """The numbers of a run whose scaler moves by its clock, whatever the session asks."""
    return run.measure(_Session())  # ty: ignore[invalid-argument-type] - a test double


def test_a_stale_sample_is_not_the_departure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Instance-b stops writing at 1000 while the scaler asks for two until 2000: the phase after
    # the departure waits for the scaler.
    clock, after = _Clock(1000), []
    scaling = _Scaling(clock, stops_writing=1000, scales_in=2000)
    stuck = {1: Counts(3, {"instance-a": 3}), 2: Counts(3, {"instance-b": 3})}
    numbers = _measured(_scaling_run(monkeypatch, clock, scaling, stuck, after))
    assert after and after[0] >= 2000
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS


def test_a_scale_in_after_the_phase_keeps_its_split(monkeypatch: pytest.MonkeyPatch) -> None:
    # Student 1 was split while both instances were in service; the scaler asks for one instance a
    # minute after the phase ended.
    clock, after = _Clock(1000), []
    scaling = _Scaling(clock, stops_writing=1059, scales_in=1060)
    split = {1: Counts(3, {"instance-a": 1, "instance-b": 2}), 2: Counts(3, {"instance-a": 3})}
    numbers = _measured(_scaling_run(monkeypatch, clock, scaling, split, after))
    assert after and after[0] >= 1060
    assert s8.SPIKE.verdict(numbers) is Verdict.FAIL
    # Instance-b stopping writing while the scaler still asks for both: a departure, not a failure.
    clock = _Clock(1000)
    scaling = _Scaling(clock, stops_writing=1000, scales_in=2000)
    with pytest.raises(s8.Departed):
        _measured(_scaling_run(monkeypatch, clock, scaling, split, []))


class _Launching:
    """A workspace and a pool-state table over instance-a, in zone-1, and instance-b, in zone-2,
    both writing and streaming answers from the start, as a launch's warm-up does: the scaler asks
    for two and reads instance-b in service only from `in_service_at`."""

    def __init__(self, clock: _Clock, in_service_at: float) -> None:
        self.clock, self.in_service_at = clock, in_service_at

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s8.INSTANCES_IN_SERVICE in query:
            return [common.Series({"pool": "mock"}, _scaler_wrote(query, end, 2 if self.clock.now >= self.in_service_at else 1))]
        if query.startswith("sum("):
            return [common.Series({}, ((end, 0.0),))]
        samples = ((end, end),) if query.startswith("timestamp(") else ((start, 0.0), (end, 20.0))
        return [
            common.Series({"instance": one, "zone": f"zone-{n}"}, samples)
            for n, one in ((1, "instance-a"), (2, "instance-b"))
        ]

    def get_item(self, *, Key: dict, ConsistentRead: bool = False) -> dict:
        in_service = 2 if self.clock.now >= self.in_service_at else 1
        return {"Item": ScalerNote(Key["pool"], 2, None, int(self.clock.now), in_service=in_service).to_item()}


class _Unlabelled(_Launching):
    """`_Launching` in service from the start, whose instance-b series have no zone label until
    `labelled_at`."""

    def __init__(self, clock: _Clock, labelled_at: float) -> None:
        super().__init__(clock, in_service_at=1000)
        self.labelled_at = labelled_at

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        series = super().read_series(query, start=start, end=end, step=step)
        if self.clock.now >= self.labelled_at:
            return series
        return [
            common.Series({name: value for name, value in one.labels.items() if (name, value) != ("zone", "zone-2")}, one.samples)
            for one in series
        ]


class _Unnoted(_Launching):
    """`_Launching` in service from the start by the scaler's gauge, whose pool has no note until
    `noted_at`."""

    def __init__(self, clock: _Clock, noted_at: float) -> None:
        super().__init__(clock, in_service_at=1000)
        self.noted_at = noted_at

    def get_item(self, *, Key: dict, ConsistentRead: bool = False) -> dict:
        return super().get_item(Key=Key, ConsistentRead=ConsistentRead) if self.clock.now >= self.noted_at else {}


@pytest.mark.parametrize(
    ("fleet", "sent_from"),
    [
        # The session's second instance writes and answers its warm-up, but is in service only from
        # 1060: nothing is sent before.
        pytest.param(lambda clock: _Launching(clock, in_service_at=1060), 1060, id="until-in-service"),
        # The workspace shows two in service, but the scaler's note says so only from 1060.
        pytest.param(lambda clock: _Unnoted(clock, noted_at=1060), 1060, id="until-noted"),
        # Both in service, so the load starts; instance-b has no zone label until 1060: it is not
        # counted as a zone of its own, and the load is held.
        pytest.param(lambda clock: _Unlabelled(clock, labelled_at=1060), 1000, id="until-zone-labelled"),
    ],
)
def test_nothing_is_sent_until_two_are_in_service_and_the_load_is_held_until_both_have_a_zone(
    monkeypatch: pytest.MonkeyPatch, fleet: Callable[[_Clock], _Launching], sent_from: int
) -> None:
    clock, started, stopped = _Clock(1000), [], []

    async def held(load: s8.loadgen.Load, keys: object, endpoint: str, *, at_once: int) -> list:
        started.append(clock.now)
        await load.sessions[0].stop.wait()  # ty: ignore[missing-attribute] - the load's held streams
        stopped.append(clock.now)
        return []

    async def wait(condition: Callable[[], bool], seconds: float, what: str) -> None:
        # The load, once it is started, sends before the job first looks.
        await asyncio.sleep(0)
        while not condition():
            clock.sleep(s8.POLL_SECONDS)

    monkeypatch.setattr(s8.loadgen, "replay", held)
    monkeypatch.setattr(s8, "_wait", wait)
    monkeypatch.setattr(s8.time, "time", lambda: clock.now)
    held_fleet = fleet(clock)
    pool = s8.Pool("mock", held_fleet, held_fleet)  # ty: ignore[invalid-argument-type] - a test double
    run = s8.Run(pool, "https://gateway.example.com", [generate_key()] * 8, 3, 8)
    assert asyncio.run(run.two_instances()) == {"instance-a": "zone-1", "instance-b": "zone-2"}
    assert (started, stopped) == ([sent_from], [1060])


def _unlabelled(client: TestClient) -> dict[str, float]:
    """The samples of an engine's metrics that have no label, by name."""
    answer = client.get("/metrics")
    assert answer.status_code == 200
    families = text_string_to_metric_families(answer.text)
    return {sample.name: sample.value for family in families for sample in family.samples if not sample.labels}


def test_the_lifecycle_agent_s_completions_are_not_a_student_s_requests() -> None:
    key = generate_key()

    async def no_delay(seconds: float) -> None:
        pass

    apps = {one: create_app(Settings(secret=key), sleep=no_delay) for one in ("instance-a", "instance-b")}
    with ExitStack() as stack:
        clients = {one: stack.enter_context(TestClient(app, client=("127.0.0.1", 40000))) for one, app in apps.items()}
        before = {one: _unlabelled(client) for one, client in clients.items()}
        # One student sends two requests, both to instance-a; the agent checks each instance meanwhile.
        load = s8.loadgen.Load((s8._session("mock", 2, 1),))
        transport = httpx.ASGITransport(app=apps["instance-a"])
        records = asyncio.run(s8.loadgen.replay(load, [key], "http://engine", at_once=1, transport=transport))
        assert [record.status for record in records] == [200, 200]
        for client in clients.values():

            def fetch(request: urllib.request.Request, timeout: float | None, client: TestClient = client) -> tuple:
                path = request.full_url.removeprefix("http://127.0.0.1:8000")
                answer = client.post(path, content=request.data, headers=dict(request.header_items()))  # ty: ignore[invalid-argument-type] - bytes
                return answer.status_code, answer.content

            assert Engine(fetch).warm_up("mock", key)
        after = {one: _unlabelled(client) for one, client in clients.items()}

    class Scrapes:
        def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
            name = query.split("{")[0]
            return [
                common.Series({"instance": one}, ((start, before[one][name]), (end, after[one][name]))) for one in after
            ]

    # Each engine counted the agent's completion as a request with a first token.
    first_tokens = "rhi_mock:time_to_first_token_seconds_count"
    assert {one: after[one][first_tokens] - before[one][first_tokens] for one in after} == {
        "instance-a": 3,
        "instance-b": 1,
    }
    assert s8.Pool("mock", Scrapes(), None).served(0.0, 1.0) == {"instance-a": 2}  # ty: ignore[invalid-argument-type] - a test double


class _Draining(_Scaling):
    """`_Scaling` whose scaler reads instance-b in service until `leaves`."""

    def __init__(self, clock: _Clock, *, stops_writing: float, scales_in: float, leaves: float) -> None:
        super().__init__(clock, stops_writing=stops_writing, scales_in=scales_in)
        self.leaves = leaves

    def in_service(self) -> int:
        return 1 if self.clock.now >= self.leaves else 2


def test_the_phase_after_waits_for_the_scaler_to_read_one_in_service(monkeypatch: pytest.MonkeyPatch) -> None:
    # Instance-b stops writing at 1000 and the scaler asks for one instance from 1060, but instance-b
    # stays in service, drained, until 1150.
    clock, after = _Clock(1000), []
    scaling = _Draining(clock, stops_writing=1000, scales_in=1060, leaves=1150)
    stuck = {1: Counts(3, {"instance-a": 3}), 2: Counts(3, {"instance-b": 3})}
    numbers = _measured(_scaling_run(monkeypatch, clock, scaling, stuck, after))
    assert after and after[0] >= 1150
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS


class _NoteBehind(_Scaling):
    """`_Scaling` whose gauge reads one in service from `scales_in`, as the workspace shows one
    writing, while the scaler's note does not say so until `noted_at`: it reads two in service
    until then, or, `without_a_note`, the pool has no note at all."""

    def __init__(self, clock: _Clock, *, noted_at: float, without_a_note: bool) -> None:
        super().__init__(clock, stops_writing=1000, scales_in=1060)
        self.noted_at, self.without_a_note = noted_at, without_a_note

    def get_item(self, *, Key: dict, ConsistentRead: bool = False) -> dict:
        if self.clock.now >= self.noted_at:
            return super().get_item(Key=Key, ConsistentRead=ConsistentRead)
        if self.without_a_note:
            return {}
        asked = 1 if self.clock.now >= self.scales_in else 2
        return {"Item": ScalerNote(Key["pool"], asked, None, int(self.clock.now), in_service=2).to_item()}


@pytest.mark.parametrize("without_a_note", [False, True], ids=["a note that reads two in service", "no note"])
def test_the_departure_waits_for_the_scaler_s_note_to_ask_for_one_and_read_one_in_service(
    monkeypatch: pytest.MonkeyPatch, without_a_note: bool
) -> None:
    # From 1060 the gauge reads one in service, and instance-b's last sample has gone stale by 1150;
    # the note says so only at 1300.
    clock, after = _Clock(1000), []
    scaling = _NoteBehind(clock, noted_at=1300, without_a_note=without_a_note)
    monkeypatch.setattr(s8, "GATEWAY_SECONDS", 0)
    stuck = {1: Counts(3, {"instance-a": 3}), 2: Counts(3, {"instance-b": 3})}
    numbers = _measured(_scaling_run(monkeypatch, clock, scaling, stuck, after))
    assert after and after[0] >= 1300
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS


def test_a_number_raised_before_a_phase_reaches_the_gateway_before_the_phase_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The scaler raised the number at 999; each gateway task reads it within a readiness cycle.
    clock, raised_at, held = _Clock(1000), 999, []

    class Raised:
        name = "mock"

        def rebalance_number(self) -> int:
            return 1

        def present(self) -> set[str]:
            return {"instance-a", "instance-b"}

    async def phase(self: s8.Run, *, first_at_once: bool) -> s8.PhaseCounts:
        held.append(clock.now >= raised_at + s8.GATEWAY_CYCLE_SECONDS)
        return STUCK

    monkeypatch.setattr(s8.time, "time", lambda: clock.now)
    monkeypatch.setattr(s8.time, "sleep", clock.sleep)
    monkeypatch.setattr(s8.Run, "phase", phase)
    run = s8.Run(Raised(), "https://gateway.example.com", [generate_key(), generate_key()], 2, 8)  # ty: ignore[invalid-argument-type] - a test double
    assert run.measured(Phase.BEFORE, first_at_once=True, in_service={"instance-a", "instance-b"}) == STUCK
    assert held == [True]


def test_firsts_the_evidence_cannot_tell_apart_are_measured_again() -> None:
    # Each student misses its first's cache, then reaches both instances: either could have opened
    # on instance-a, and the two ways give the students different counts.
    windows = {student: Counts(3, {"instance-a": 2, "instance-b": 1}) for student in s8.STUDENTS}
    counts = s8.with_openings({"instance-a": 1, "instance-b": 1}, windows, dict.fromkeys(s8.STUDENTS, False))
    assert not any(held.adds_up() for held in counts.values())
    with pytest.raises(s8.Disturbed):
        s8.measure_undisturbed(Phase.BEFORE, lambda: counts, lambda: 0, attempts=1, log=lambda _: None)


class _Unread(_Scaling):
    """`_Scaling` whose workspace has no reading of the scaler's gauge from `scales_in` until `reads_one`."""

    def __init__(self, clock: _Clock, *, stops_writing: float, scales_in: float, reads_one: float) -> None:
        super().__init__(clock, stops_writing=stops_writing, scales_in=scales_in)
        self.reads_one = reads_one

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s8.INSTANCES_IN_SERVICE in query and self.scales_in <= self.clock.now < self.reads_one:
            return []
        return super().read_series(query, start=start, end=end, step=step)


def test_the_phase_after_waits_for_a_reading_of_one_in_service(monkeypatch: pytest.MonkeyPatch) -> None:
    # The scaler asks for one instance from 1060 and instance-b no longer writes, but the workspace
    # has no reading of the instances in service until 1150: the scale-in is not yet seen complete.
    clock, after = _Clock(1000), []
    scaling = _Unread(clock, stops_writing=1000, scales_in=1060, reads_one=1150)
    stuck = {1: Counts(3, {"instance-a": 3}), 2: Counts(3, {"instance-b": 3})}
    numbers = _measured(_scaling_run(monkeypatch, clock, scaling, stuck, after))
    assert after and after[0] >= 1150
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS


def test_a_miscounted_window_is_never_made_up_by_another() -> None:
    # The window of the two firsts counts one; student 2's window counts one too many.
    windows = {1: Counts(1, {"instance-b": 1}), 2: Counts(1, {"instance-a": 2})}
    counts = s8.with_openings({"instance-b": 1}, windows, dict.fromkeys(s8.STUDENTS, False))
    with pytest.raises(s8.Disturbed):
        s8.measure_undisturbed(Phase.BEFORE, lambda: counts, lambda: 0, attempts=1, log=lambda _: None)


def test_scale_out_requires_a_current_in_service_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    written = now - s8.FRESH_SECONDS - 15
    monkeypatch.setattr(s8.time, "time", lambda: now)

    class Lookback:
        """Both collectors write; a range query still returns the scaler's last gauge, at each step's
        own time, from before the freshness window."""

        def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
            if s8.INSTANCES_IN_SERVICE not in query:
                return [common.Series({"instance": one}, ((end, now),)) for one in ("instance-a", "instance-b")]
            if query.startswith("timestamp("):
                return [common.Series({"pool": "mock"}, ((end, written),))]
            return [common.Series({"pool": "mock"}, ((start, 2.0), (end, 2.0)))]

    pool = s8.Pool("mock", Lookback(), _Table(2, 0))  # ty: ignore[invalid-argument-type] - a test double
    with pytest.raises(s8.Unseen):
        pool.present()
    written = now
    assert pool.present() == {"instance-a", "instance-b"}


def test_scale_out_ends_only_with_exactly_two_in_service(monkeypatch: pytest.MonkeyPatch) -> None:
    poll, stopped_at = 0, []

    class Three:
        """The session's two in service, then three at the load's first look, two after; only two
        streamed."""

        name = "mock"

        def present(self) -> set[str]:
            return {"instance-a", "instance-b", "instance-c"} if poll == 2 else {"instance-a", "instance-b"}

        def noted(self) -> tuple[int, int]:
            return (2, 2)

        def serving(self) -> dict[str, str]:
            return {"instance-a": "zone-1", "instance-b": "zone-2"}

        def busy(self) -> int:
            return 0

    async def held(load: s8.loadgen.Load, keys: object, endpoint: str, *, at_once: int) -> list:
        await load.sessions[0].stop.wait()  # ty: ignore[missing-attribute] - the load's held streams
        stopped_at.append(poll)
        return []

    async def wait(condition: Callable[[], bool], seconds: float, what: str) -> None:
        nonlocal poll
        for _ in range(3):
            poll += 1
            await asyncio.sleep(0)
            if condition():
                return
        raise TimeoutError(what)

    monkeypatch.setattr(s8.loadgen, "replay", held)
    monkeypatch.setattr(s8, "_wait", wait)
    run = s8.Run(Three(), "https://gateway.example.com", [generate_key()] * 8, 1, 8)  # ty: ignore[invalid-argument-type] - a test double
    assert asyncio.run(run.two_instances()) == {"instance-a": "zone-1", "instance-b": "zone-2"}
    assert stopped_at == [3]


@pytest.mark.parametrize(
    ("in_service", "student_2_on"),
    [
        # A third instance joins before the students' requests, and student 2 is on it.
        pytest.param({"instance-a", "instance-b", "instance-c"}, "instance-c", id="third-in-service"),
        # Only the two are in service at the phase's end, but a third served a measured request.
        pytest.param({"instance-a", "instance-b"}, "instance-c", id="third-served"),
    ],
)
def test_a_third_instance_cannot_prove_two_instance_stickiness(
    monkeypatch: pytest.MonkeyPatch, in_service: set[str], student_2_on: str
) -> None:
    class Pool:
        name = "mock"

        def rebalance_number(self) -> int:
            return 0

        def present(self) -> set[str]:
            return in_service

    async def two_instances(self: s8.Run) -> dict[str, str]:
        return {"instance-a": "zone-1", "instance-b": "zone-2"}

    async def phase(self: s8.Run, *, first_at_once: bool) -> s8.PhaseCounts:
        return {1: Counts(2, {"instance-a": 2}), 2: Counts(2, {student_2_on: 2})}

    monkeypatch.setattr(s8, "GATEWAY_SECONDS", 0)
    monkeypatch.setattr(s8.Run, "two_instances", two_instances)
    monkeypatch.setattr(s8.Run, "phase", phase)
    run = s8.Run(Pool(), "https://gateway.example.com", [generate_key(), generate_key()], 1, 8)  # ty: ignore[invalid-argument-type] - a test double
    with pytest.raises(s8.Departed):
        run.measure(_Session())  # ty: ignore[invalid-argument-type] - a test double


class _Minutely(_Scaling):
    """`_Scaling` whose scaler runs once a minute from 1000, its sample of each run reaching the
    workspace two seconds after the run's start, which it carries: it asks for one instance, and
    reads one in service, from its run at 1180; instance-b stops writing at 1182."""

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s8.INSTANCES_IN_SERVICE in query:
            written = 1000 + 60 * ((int(end) - 1002) // 60)
            value = written if query.startswith("timestamp(") else (1 if written >= 1180 else 2)
            return [common.Series({"pool": "mock"}, ((end, value),))]
        active = ("instance-a",) if end >= 1182 else ("instance-a", "instance-b")
        return [common.Series({"instance": one}, ((end, end),)) for one in active]


def test_the_scaler_s_next_sample_on_its_way_does_not_discard_a_stable_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    # The phase ends at 1061: the scaler's last visible sample is from its run at 1000, and its run
    # at 1060 has not yet reached the workspace. Both instances are in service.
    clock, after = _Clock(1061), []
    scaling = _Minutely(clock, stops_writing=1182, scales_in=1180)
    monkeypatch.setattr(s8, "GATEWAY_SECONDS", 0)
    stuck = {1: Counts(3, {"instance-a": 3}), 2: Counts(3, {"instance-b": 3})}
    numbers = _measured(_scaling_run(monkeypatch, clock, scaling, stuck, after))
    assert after and after[0] >= 1182
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS


# The session the job books, lowers and stops.

HOURS = 12
BOTH = {"instance-a": "zone-1", "instance-b": "zone-2"}


@pytest.fixture
def table() -> Iterator[Any]:
    """The pool-state table, in moto, with the key schema of `rhi_common.tables.POOL_STATE` and no item."""
    with mock_aws():
        keys = [(POOL_STATE.partition_key, "HASH"), (POOL_STATE.sort_key, "RANGE")]
        yield boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="pool-state-under-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys if key is not None
            ],
            KeySchema=[{"AttributeName": key.name, "KeyType": kind} for key, kind in keys if key is not None],
        )


def _stored(table: Any) -> Session | None:
    item = table.get_item(Key={"pool": "mock", "record": SESSION_RECORD}, ConsistentRead=True).get("Item")
    return None if item is None else Session.from_item(item)


class _Following(_Departing):
    """`_Departing` whose scaler follows at once the session stored in the pool-state table `table`:
    both instances are in service, and serve, while it asks for two, and instance-b has left once it
    asks for one."""

    def __init__(self, table: Any, served: list[dict[str, int]]) -> None:
        super().__init__(served)
        self.table = table

    def noted(self) -> tuple[int, int]:
        session = _stored(self.table)
        asked = 0 if session is None else session.instances
        return (asked, asked)

    def present(self) -> set[str]:
        return set(BOTH) if self.noted() == (2, 2) else {"instance-a"}

    def serving(self) -> dict[str, str]:
        return BOTH

    def busy(self) -> int:
        return 0


def _booking_run(
    monkeypatch: pytest.MonkeyPatch,
    pool: _Following,
    clock: _Clock,
    kept: tuple[bool, bool] = (True, True),
    on_rest: Callable[[], None] = lambda: None,
) -> tuple[s8.Run, list[Session | None]]:
    """A run on `pool`, on the time of `clock`, with the pool's session as it was stored each time
    the job sent anything: the load, then each replay of the students' requests."""
    keys = [generate_key() for _ in range(8)]
    sent: list[Session | None] = []
    answered = _replay(keys, kept, on_rest)

    async def replay(load: s8.loadgen.Load, supplied: list[str], endpoint: str, *, at_once: int) -> list:
        sent.append(_stored(pool.table))
        (first, *_) = load.sessions
        if isinstance(first, s8.HeldStream):
            await first.stop.wait()
            return []
        return await answered(load, supplied, endpoint, at_once=at_once)

    async def wait(condition: Callable[[], bool], seconds: float, what: str) -> None:
        # The load, once it is started, sends before the job first looks.
        await asyncio.sleep(0)
        assert condition()

    monkeypatch.setattr(s8.loadgen, "replay", replay)
    monkeypatch.setattr(s8, "_wait", wait)
    monkeypatch.setattr(s8, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(s8.time, "time", lambda: clock.now)
    monkeypatch.setattr(s8.time, "sleep", clock.sleep)
    return s8.Run(pool, "https://gateway.example.com", keys, 2, 3), sent  # ty: ignore[invalid-argument-type] - a test double


FIRSTS = {"instance-a": 1, "instance-b": 1}
ON_THE_REMAINING = [{"instance-a": 2}, {"instance-a": 2}]


@pytest.mark.parametrize(
    ("served", "kept", "outcome"),
    [
        # Student 1 is on instance-b, which leaves, and then reaches instance-a.
        pytest.param([FIRSTS, {"instance-b": 2}, {"instance-a": 2}], (True, True), Verdict.PASS, id="passed"),
        # Student 2 opens on instance-a and moves to instance-b while both are in service.
        pytest.param([FIRSTS, {"instance-b": 2}, {"instance-b": 2}], (True, False), Verdict.FAIL, id="failed"),
        # Both students on instance-a, which stays: neither had to move.
        pytest.param([{"instance-a": 2}] * 3, (True, True), s8.NotExercised, id="not measured"),
    ],
)
def test_the_job_books_two_lowers_to_one_for_the_departure_and_stops_its_session(
    monkeypatch: pytest.MonkeyPatch,
    table: Any,
    served: list[dict[str, int]],
    kept: tuple[bool, bool],
    outcome: Verdict | type[Exception],
) -> None:
    clock = _Clock(1000)
    run, sent = _booking_run(monkeypatch, _Following(table, [*served, *ON_THE_REMAINING]), clock, kept)

    if isinstance(outcome, Verdict):
        numbers = run.in_session(minutes=90, max_instances=2, session_max_hours=HOURS)
        assert s8.SPIKE.verdict(numbers) is outcome
    else:
        with pytest.raises(outcome):
            run.in_session(minutes=90, max_instances=2, session_max_hours=HOURS)

    # Booked before the first thing sent, the load: two instances, from now, for the minutes given.
    assert sent[0] == Session(
        pool="mock", instances=2, starts_at=1000, ends_at=1000 + 90 * 60, set_by="spike/s8_stickiness", set_at=1000
    )
    # Two through the load and the phase before the departure; one when the students send again.
    lowered = sent[-1]
    assert lowered is not None
    assert [None if held is None else held.instances for held in sent] == [2, 2, 2, 2, 1, 1]
    assert lowered == dataclasses.replace(sent[0], instances=1, set_at=lowered.set_at)
    # Stopped when the run ended: the session the job last wrote, ended now.
    now = int(clock.now)
    ended = _stored(table)
    assert ended == dataclasses.replace(lowered, ends_at=now, set_at=now)
    assert not sessions.is_active(ended, now, session_max_hours=HOURS)


@pytest.mark.parametrize(
    ("held", "max_instances"),
    [
        pytest.param(Session("mock", 1, 1600, 5200, "admin", 900), 2, id="a session booked"),
        pytest.param(Session("mock", 1, 400, 1001, "admin", 300), 2, id="a session active"),
        pytest.param(None, 1, id="fewer than two instances allowed"),
    ],
)
def test_a_pool_the_job_may_not_book_is_left_as_it_is_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, table: Any, held: Session | None, max_instances: int
) -> None:
    if held is not None:
        table.put_item(Item=held.to_item())
    run, sent = _booking_run(monkeypatch, _Following(table, []), _Clock(1000))

    with pytest.raises(s8.Refused):
        run.in_session(minutes=90, max_instances=max_instances, session_max_hours=HOURS)

    assert sent == [] and _stored(table) == held


def test_a_session_someone_else_changed_is_neither_lowered_nor_stopped(
    monkeypatch: pytest.MonkeyPatch, table: Any
) -> None:
    clock, theirs = _Clock(1000), []

    def someone_changes() -> None:
        # During the phase before the departure, once: the admin keeps the session half an hour longer.
        mine = _stored(table)
        if mine is not None and not theirs:
            theirs.append(dataclasses.replace(mine, ends_at=mine.ends_at + 1800, set_by="admin", set_at=int(clock.now)))
            table.put_item(Item=theirs[0].to_item())

    pool = _Following(table, [FIRSTS, {"instance-b": 2}, {"instance-a": 2}])
    run, sent = _booking_run(monkeypatch, pool, clock, on_rest=someone_changes)

    with pytest.raises(s8.Taken):
        run.in_session(minutes=90, max_instances=2, session_max_hours=HOURS)

    # The run stopped where it would have lowered the session, which is as the admin wrote it.
    assert len(sent) == 4
    assert theirs and _stored(table) == theirs[0]


class _ClosedLog(io.StringIO):
    """A build log that takes no more lines."""

    def write(self, text: str) -> int:
        raise BrokenPipeError("the build log is closed")


def test_a_log_line_that_fails_once_the_pool_is_booked_still_stops_the_session(
    monkeypatch: pytest.MonkeyPatch, table: Any
) -> None:
    run, sent = _booking_run(monkeypatch, _Following(table, []), _Clock(1000))

    with contextlib.redirect_stdout(_ClosedLog()), pytest.raises(BrokenPipeError):
        run.in_session(minutes=90, max_instances=2, session_max_hours=HOURS)

    ended = _stored(table)
    assert sent == [] and ended is not None and ended.set_by == "spike/s8_stickiness"
    assert not sessions.is_active(ended, 1000, session_max_hours=HOURS)


@pytest.mark.parametrize(
    ("reading", "outcome", "instances"),
    [
        # Once the phase before the departure is read: the job would lower a session that has ended.
        pytest.param(3, s8.Taken, 2, id="before the lowering"),
        # Once the last requests were answered, while the job reads what they reached.
        pytest.param(5, Verdict.PASS, 1, id="at the last reading"),
    ],
)
def test_a_session_whose_minutes_ran_out_is_not_lowered_and_keeps_a_run_already_answered(
    monkeypatch: pytest.MonkeyPatch, table: Any, reading: int, outcome: Verdict | type[Exception], instances: int
) -> None:
    clock, readings = _Clock(1000), itertools.count(1)
    pool = _Following(table, [FIRSTS, {"instance-b": 2}, {"instance-a": 2}, *ON_THE_REMAINING])
    read = pool.served

    def served(start: float, end: float) -> dict[str, int]:
        # The session's ten minutes have passed at that reading, and the scaler has not yet acted.
        if next(readings) == reading:
            clock.now = 1000 + 10 * 60 + 1
        return read(start, end)

    monkeypatch.setattr(pool, "served", served)
    run, _ = _booking_run(monkeypatch, pool, clock)

    if isinstance(outcome, Verdict):
        assert s8.SPIKE.verdict(run.in_session(minutes=10, max_instances=2, session_max_hours=HOURS)) is outcome
    else:
        with pytest.raises(outcome):
            run.in_session(minutes=10, max_instances=2, session_max_hours=HOURS)

    # The session ended by itself, at the end the job booked: the job's stop found nothing to write.
    ended = _stored(table)
    assert ended is not None and (ended.instances, ended.ends_at) == (instances, 1600)
    assert not sessions.is_active(ended, int(clock.now), session_max_hours=HOURS)


def test_the_job_does_not_start_without_the_minutes_of_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[object] = []
    monkeypatch.setattr(common, "run_spike", lambda *given, **named: started.append(given))
    with pytest.raises(ValueError, match="RHI_SPIKE_RUN_MINUTES"):
        s8.main({})
    assert started == []


@dataclasses.dataclass
class _Job:
    """What the job did with its session's work replaced: the spikes it started, the runs that
    booked a session, and the bounds they were asked to book under."""

    started: list[str] = dataclasses.field(default_factory=list)
    booked: list[s8.Run] = dataclasses.field(default_factory=list)
    asked: dict[str, int] = dataclasses.field(default_factory=dict)


def _job(monkeypatch: pytest.MonkeyPatch, example: config.Config) -> _Job:
    job = _Job()

    def in_session(self: s8.Run, **bounds: int) -> dict[str, int | float]:
        job.booked.append(self)
        job.asked.update(bounds)
        return {}

    def run_spike(spike: object, name: str, measure: Callable[[], object], *, environ: object) -> object:
        job.started.append(name)
        return measure()

    aws = SimpleNamespace(get_credentials=lambda: None, resource=lambda _: SimpleNamespace(Table=lambda name: name))
    monkeypatch.setattr(s8.config, "read_ssm", lambda: example)
    monkeypatch.setattr(s8.boto3, "Session", lambda: aws)
    monkeypatch.setattr(common, "canary_keys", lambda: SimpleNamespace(students=[generate_key()]))
    monkeypatch.setattr(common, "run_spike", run_spike)
    monkeypatch.setattr(s8.Run, "in_session", in_session)
    return job


def _environ(endpoint: str) -> dict[str, str]:
    return {
        "RHI_SPIKE_RUN_MINUTES": "75",
        "RHI_S8_REQUESTS": "5",
        "RHI_SPIKE_ENDPOINT": endpoint,
        "RHI_SPIKE_WORKSPACE_URL": "https://workspace.example.com",
        "RHI_SPIKE_POOL_STATE_TABLE": "pool-state-under-test",
        "AWS_REGION": "us-east-1",
    }


def test_the_job_books_for_the_minutes_it_is_given_under_the_bounds_of_the_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = config.load({"RHI_CONFIG": "example"})
    (mock,) = [pool for pool in example.pools if pool.name == "mock"]
    job = _job(monkeypatch, example)

    s8.main(_environ("https://gateway.example.com"))

    assert job.asked == {
        "minutes": 75,
        "max_instances": mock.max_instances,
        "session_max_hours": example.session_max_hours,
    }


def test_the_job_given_the_gateway_stacks_host_name_reaches_the_gateway_over_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(monkeypatch, config.load({"RHI_CONFIG": "example"}))

    s8.main(_environ("gateway.example.com"))

    # A run sends every request to its endpoint (`loadgen.replay`).
    assert [run.endpoint for run in job.booked] == ["https://gateway.example.com"]


def test_the_job_given_an_http_address_starts_nothing_and_books_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(monkeypatch, config.load({"RHI_CONFIG": "example"}))

    with pytest.raises(ValueError, match="https"):
        s8.main(_environ("http://gateway.example.com"))

    assert (job.started, job.booked) == ([], [])
