"""Spike S8's verdict (`spikes/s8_stickiness.py`): whether a student's requests stayed on one instance,
from recorded counts of what each instance served. No number here is a measurement; the instance
names are made up.
"""

from __future__ import annotations

import asyncio
import dataclasses
import urllib.request
from contextlib import ExitStack
from typing import TYPE_CHECKING

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from starlette.testclient import TestClient

import common
import s8_stickiness as s8
from rhi_common.keys import generate_key
from rhi_common.records import ScalerNote
from rhi_lifecycle.launch import Engine
from rhi_mock_engine.app import Settings, create_app
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
    """A workspace in which instance-a and instance-b both wrote a sample just now, and the scaler
    read both in service."""

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s8.INSTANCES_IN_SERVICE in query:
            return [common.Series({"pool": "mock"}, ((end, 2),))]
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
    `stops_writing`, and the scaler asks for one instance, and reads one in service, from `scales_in`."""

    def __init__(self, clock: _Clock, *, stops_writing: float, scales_in: float) -> None:
        self.clock, self.stops_writing, self.scales_in = clock, stops_writing, scales_in

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s8.INSTANCES_IN_SERVICE in query:
            return [common.Series({"pool": "mock"}, ((end, 1 if self.clock.now >= self.scales_in else 2),))]
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


class _Launching:
    """A workspace and a pool-state table over instance-a, in zone-1, and instance-b, in zone-2,
    both writing and streaming answers from the start, as a launch's warm-up does: the scaler asks
    for two and reads instance-b in service only from `in_service_at`."""

    def __init__(self, clock: _Clock, in_service_at: float) -> None:
        self.clock, self.in_service_at = clock, in_service_at

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s8.INSTANCES_IN_SERVICE in query:
            return [common.Series({"pool": "mock"}, ((end, 2 if self.clock.now >= self.in_service_at else 1),))]
        if query.startswith("sum("):
            return [common.Series({}, ((end, 0.0),))]
        samples = ((end, end),) if query.startswith("timestamp(") else ((start, 0.0), (end, 20.0))
        return [
            common.Series({"instance": one, "zone": f"zone-{n}"}, samples)
            for n, one in ((1, "instance-a"), (2, "instance-b"))
        ]

    def get_item(self, *, Key: dict, ConsistentRead: bool = False) -> dict:
        return {"Item": ScalerNote(Key["pool"], 2, None, int(self.clock.now)).to_item()}


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


@pytest.mark.parametrize(
    "fleet",
    [
        pytest.param(lambda clock: _Launching(clock, in_service_at=1060), id="until-in-service"),
        # Instance-b has no zone label until 1060: it is not counted as a zone of its own.
        pytest.param(lambda clock: _Unlabelled(clock, labelled_at=1060), id="until-zone-labelled"),
    ],
)
def test_the_load_is_held_until_the_scaler_reads_two_instances_in_service(
    monkeypatch: pytest.MonkeyPatch, fleet: Callable[[_Clock], _Launching]
) -> None:
    clock, stopped = _Clock(1000), []

    async def held(load: s8.loadgen.Load, keys: object, endpoint: str, *, at_once: int) -> list:
        await load.sessions[0].stop.wait()  # ty: ignore[unresolved-attribute] - the load's held streams
        stopped.append(clock.now)
        return []

    async def wait(condition: Callable[[], bool], seconds: float, what: str) -> None:
        while not condition():
            clock.sleep(s8.POLL_SECONDS)

    monkeypatch.setattr(s8.loadgen, "replay", held)
    monkeypatch.setattr(s8, "_wait", wait)
    monkeypatch.setattr(s8.time, "time", lambda: clock.now)
    held_fleet = fleet(clock)
    pool = s8.Pool("mock", held_fleet, held_fleet)  # ty: ignore[invalid-argument-type] - a test double
    run = s8.Run(pool, "https://gateway.example.com", [generate_key()] * 8, 3, 8)
    assert asyncio.run(run.two_instances()) == {"instance-a": "zone-1", "instance-b": "zone-2"}
    assert stopped == [1060]


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

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        if s8.INSTANCES_IN_SERVICE in query:
            return [common.Series({"pool": "mock"}, ((end, 1 if self.clock.now >= self.leaves else 2),))]
        return super().read_series(query, start=start, end=end, step=step)


def test_the_phase_after_waits_for_the_scaler_to_read_one_in_service(monkeypatch: pytest.MonkeyPatch) -> None:
    # Instance-b stops writing at 1000 and the scaler asks for one instance from 1060, but instance-b
    # stays in service, drained, until 1150.
    clock, after = _Clock(1000), []
    scaling = _Draining(clock, stops_writing=1000, scales_in=1060, leaves=1150)
    stuck = {1: Counts(3, {"instance-a": 3}), 2: Counts(3, {"instance-b": 3})}
    numbers = _scaling_run(monkeypatch, clock, scaling, stuck, after).measure()
    assert after and after[0] >= 1150
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
    numbers = _scaling_run(monkeypatch, clock, scaling, stuck, after).measure()
    assert after and after[0] >= 1150
    assert s8.SPIKE.verdict(numbers) is Verdict.PASS


def test_a_miscounted_window_is_never_made_up_by_another() -> None:
    # The window of the two firsts counts one; student 2's window counts one too many.
    windows = {1: Counts(1, {"instance-b": 1}), 2: Counts(1, {"instance-a": 2})}
    counts = s8.with_openings({"instance-b": 1}, windows, dict.fromkeys(s8.STUDENTS, False))
    with pytest.raises(s8.Disturbed):
        s8.measure_undisturbed(Phase.BEFORE, lambda: counts, lambda: 0, attempts=1, log=lambda _: None)
