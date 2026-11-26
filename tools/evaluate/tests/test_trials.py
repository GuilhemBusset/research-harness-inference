"""The live trials of a plan (`evaluate.trials`), at low load and under class contention
(docs/design/agent-session-evaluation.md, "Workload and measurement protocol" and "Course quality
and the grader"): which seat the live attempt takes, what keeps sending beside it and until when,
what each attempt counts as, and what is left once it ended.

The background is the real replay driver against a made-up gateway on a clock the test moves; the
harnesses and graders are doubles. Nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import json
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from evaluate import plan, replay, trials, workers
from evaluate.adapters import Delegation
from evaluate.contracts import (
    FailureReason,
    Family,
    Grade,
    InvalidReason,
    Phase,
    Profile,
    RequestOutcome,
    Route,
    TrialOutcome,
    Usage,
)
from evaluate.course import analysis, optimization, research, revision
from evaluate.forwarding import Evidence, Measurement
from evaluate.streams import Attempt, Attempts
from rhi_common.keys import generate_key

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Sequence

    from evaluate.contracts import TrialIdentity

COURSE = plan.load_suite()
SHORT: dict[str, Any] = {
    "opening_tokens": 8,
    "transcript_tokens": [16, 32],
    "turn_input_tokens": [2, 6],
    "turn_output_tokens": 4,
    "tool_delay_ms": [500, 1000],
}
# Every family, one instance each on the anchor, at both loads, once.
PROFILE = replace(
    COURSE.profiles[Profile.QUALIFY],
    repetitions=1,
    attempts=1,
    anchor_only=True,
    instances_per_task=1,
    table={"repetitions": 1, "attempts": 1, "harnesses": "anchor", "instances_per_task": 1},
)
SPEC = replace(
    COURSE,
    protocol=COURSE.protocol | {"replay": COURSE.protocol["replay"] | SHORT},
    profiles=COURSE.profiles | {Profile.QUALIFY: PROFILE},
)
CLASS = plan.expand(SPEC, Profile.QUALIFY, 8)
ALONE = plan.expand(SPEC, Profile.QUALIFY, 1)
# Two trials in each cell, one after the other.
TWICE = plan.expand(
    replace(
        SPEC,
        profiles=SPEC.profiles
        | {Profile.QUALIFY: replace(PROFILE, instances_per_task=2, table=PROFILE.table | {"instances_per_task": 2})},
    ),
    Profile.QUALIFY,
    8,
)
KEYS = [generate_key() for _ in range(CLASS.canary_identities)]
START = 7_000
ANSWER_MS = 1_000
TRIAL_MS = 20_000
YIELDS = 100


class Clock:
    """A clock the test moves: each time every task waits, the time moves to the earliest wait."""

    def __init__(self) -> None:
        self.at = START
        self._waits: list[tuple[int, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def now_ms(self) -> int:
        return self.at

    async def sleep_ms(self, ms: int) -> None:
        wait = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waits, (self.at + max(ms, 0), next(self._order), wait))
        await wait

    def drive[Result](self, work: Coroutine[Any, Any, Result]) -> Result:
        async def driven() -> Result:
            task = asyncio.ensure_future(work)
            while not task.done():
                for _ in range(YIELDS):
                    await asyncio.sleep(0)
                while self._waits and self._waits[0][2].cancelled():
                    heapq.heappop(self._waits)
                if task.done():
                    break
                assert self._waits, "a task waits on something the clock does not end"
                ends, _, wait = heapq.heappop(self._waits)
                self.at = max(self.at, ends)
                wait.set_result(None)
            return task.result()

        return asyncio.run(driven())


def event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps({'type': name} | data)}\n\n".encode()


class Gateway(httpx.AsyncBaseTransport):
    """A made-up gateway: it knows a student by the key, and answers each request after `ANSWER_MS`."""

    def __init__(self, clock: Clock, keys: Sequence[str]) -> None:
        self.clock, self.keys = clock, list(keys)
        self.arrived: list[tuple[int, int]] = []
        self.open: Counter[int] = Counter()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        student = self.keys.index(request.headers["authorization"].removeprefix("Bearer "))
        self.arrived.append((self.clock.now_ms(), student))

        async def stream() -> AsyncIterator[bytes]:
            self.open[student] += 1
            try:
                yield event("message_start", {"message": {"usage": {"input_tokens": 30, "cache_read_input_tokens": 0}}})
                await self.clock.sleep_ms(ANSWER_MS)
                yield event("content_block_delta", {"delta": {"type": "text_delta", "text": "made-up answer"}})
                yield event("message_delta", {"usage": {"output_tokens": 4}})
                yield event("message_stop", {})
            finally:
                self.open[student] -= 1

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())


@dataclass(frozen=True)
class Act:
    """What a harness double does in an attempt: how long it works, its requests to the gateway (from
    and to, after its start), what it reports, and the answer it leaves."""

    duration_ms: int = TRIAL_MS
    spans: tuple[tuple[int, int], ...] = ((0, 2_000), (2_000, 4_000))
    failure: FailureReason | None = None
    delegation: Delegation | None = None
    answer: str = "right"
    turns: int = 3
    retries: int = 1
    output_tokens: int = 900


def evidence(started: int, ended: int, act: Act) -> Evidence:
    """A forwarder's evidence of an attempt that made the act's requests."""
    each = act.output_tokens // len(act.spans)
    requests = tuple(
        Measurement(
            attempt=Attempt(
                route=Route.MESSAGES,
                retry=0,
                status=200,
                outcome=RequestOutcome.COMPLETED,
                sent_ms=started + sent,
                first_output_ms=None,
                first_answer_ms=None,
                last_output_ms=None,
                ended_ms=started + done,
                usage=Usage(input_tokens=100, cached_input_tokens=0, output_tokens=each),
                output_counted=True,
            ),
            received_ms=started + sent,
            side_call=False,
            closed_by_client=False,
            cancelled=False,
            allowance_tokens=each,
            charged_tokens=each,
        )
        for sent, done in act.spans
    )
    return Evidence(
        started_ms=started,
        ended_ms=ended,
        failure=act.failure,
        invalid=None,
        model_turns=act.turns,
        side_calls=1,
        retries=act.retries,
        refusals=0,
        generated_tokens=act.output_tokens,
        open_reservations=0,
        tally=Attempts().tally(),
        requests=requests,
    )


def family(trial: TrialIdentity) -> Family:
    return Family(trial.instance_id.split(".")[0])


@dataclass
class Harnesses:
    """A harness double: each attempt works on the clock for its act's time, or until cancelled,
    and leaves its answer in a folder of its own, which is removed when the attempt is released."""

    clock: Clock
    root: Path
    acts: dict[Family, Act] = field(default_factory=dict)
    keys: list[str] = field(default_factory=list)
    open: int = 0
    most_open: int = 0
    released: int = 0

    @contextlib.asynccontextmanager
    async def attempt(self, trial: TrialIdentity, key: str, cancel: asyncio.Event) -> AsyncGenerator[trials.Work]:
        act = self.acts.get(family(trial), Act())
        self.keys.append(key)
        self.open += 1
        self.most_open = max(self.most_open, self.open)
        folder = self.root / f"attempt-{len(self.keys)}"
        folder.mkdir()
        try:
            started = self.clock.now_ms()
            ends = [asyncio.ensure_future(self.clock.sleep_ms(act.duration_ms)), asyncio.ensure_future(cancel.wait())]
            try:
                await asyncio.wait(ends, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for one in ends:
                    one.cancel()
                await asyncio.gather(*ends, return_exceptions=True)
            (folder / "answer.txt").write_text(act.answer, encoding="utf-8")
            yield trials.Work(evidence(started, self.clock.now_ms(), act), folder, act.delegation)
        finally:
            shutil.rmtree(folder)
            self.open -= 1
            self.released += 1


def grade(trial: TrialIdentity, work: trials.Work) -> Grade:
    """A grader double: every required property of the task holds but the last, which is whether
    the answer is right."""
    right = (work.artifacts / "answer.txt").read_text(encoding="utf-8") == "right"
    required = trials.task_of(CLASS, trial).required_properties
    return Grade(properties=(True,) * (required - 1) + (right,), within_limits=True)


def broken(trial: TrialIdentity, work: trials.Work) -> Grade:
    raise RuntimeError("the grader's fixture is missing")


GRADERS: dict[Family, trials.Grader] = dict.fromkeys(Family, grade)


def cells(planned: plan.Plan, *families: Family, phase: Phase | None = None) -> list[int]:
    """The live cells of the plan that run those families, of that phase if given."""
    return [
        cell
        for cell in trials.live_cells(planned)
        if family(planned.workload.trials[planned.cells[cell].trials[0]]) in families
        and phase in (None, planned.cells[cell].identity.phase)
    ]


def run(
    planned: plan.Plan,
    chosen: Sequence[int],
    tmp_path: Path,
    *,
    acts: dict[Family, Act] | None = None,
    graders: dict[Family, trials.Grader] = GRADERS,
    cancel_at: int | None = None,
    task_cancelled_at: int | None = None,
    breaks_at: int | None = None,
) -> tuple[trials.Run | None, Gateway, Harnesses]:
    """Runs those cells: what the run returned (`None` when its task was cancelled), what the
    gateway saw and what the harnesses did. With `breaks_at`, the replay's driver raises once the
    first background request has been open that long."""
    clock = Clock()
    pool = Gateway(clock, KEYS)
    harnesses = Harnesses(clock, tmp_path, acts or {})
    transport: httpx.AsyncBaseTransport = pool
    if breaks_at is not None:

        async def broken(request: httpx.Request) -> httpx.Response:
            await clock.sleep_ms(breaks_at)
            raise RuntimeError("the replay driver broke")

        transport = httpx.MockTransport(broken)
    gateway = replay.Gateway("https://gateway.example.com", "coursework", clock, transport)

    async def work() -> trials.Run | None:
        cancel = asyncio.Event()
        before = asyncio.all_tasks()

        async def at(ms: int, then: Callable[[], object]) -> None:
            await clock.sleep_ms(ms - clock.now_ms())
            then()

        ran = asyncio.ensure_future(trials.run(planned, gateway, KEYS, harnesses, graders, cells=chosen, cancel=cancel))
        if cancel_at is not None:
            asyncio.ensure_future(at(cancel_at, cancel.set))
        if task_cancelled_at is None:
            return await ran
        await at(task_cancelled_at, ran.cancel)
        with pytest.raises(asyncio.CancelledError):
            await ran
        # Nothing the run started is still running.
        assert asyncio.all_tasks() == before
        return None

    return clock.drive(work()), pool, harnesses


def test_a_hands_on_and_a_parallel_task_run_at_both_loads_with_one_live_seat(tmp_path: Path) -> None:
    # The analysis worker reported two delegated workers that ran one after the other; the
    # optimization trial sent a side query while its turn was open.
    acts = {
        Family.ANALYSIS: Act(spans=((0, 2_000), (2_000, 4_000)), delegation=Delegation(2, overlapped=False)),
        Family.OPTIMIZATION: Act(spans=((0, 3_000), (1_000, 2_000), (5_000, 6_000))),
    }
    chosen = cells(CLASS, Family.ANALYSIS, Family.OPTIMIZATION)
    ran, pool, harnesses = run(CLASS, chosen, tmp_path, acts=acts)
    assert ran is not None

    phases = Counter(cell.cell.identity.phase for cell in ran.cells)
    assert phases == {Phase.LIVE_LOW_LOAD: 2, Phase.LIVE_CLASS_LOAD: 2}
    # The live attempt always takes the first seat, and the background never sends under its key.
    assert harnesses.keys == [KEYS[0]] * 4
    assert 0 not in {student for _at, student in pool.arrived}
    for cell in ran.cells:
        (executed,) = cell.trials
        record = executed.record
        background = executed.background
        assert background is not None
        assert record.student == trials.LIVE_STUDENT
        # The trial lies inside its background, and inside its cell's record.
        assert background.record.started_ms <= record.started_ms
        assert record.ended_ms <= background.record.admissions_ended_ms <= background.record.ended_ms
        assert cell.background.record == background.record
        if cell.cell.identity.phase is Phase.LIVE_LOW_LOAD:
            assert executed.loads == (plan.Role.LOW_LOAD,)
            assert background.requests == ()
            continue
        assert executed.loads == (plan.Role.CLASS_LOAD,)
        # An eight-seat class: seven background seats, each in the family's shape.
        assert cell.background_students == frozenset(range(1, 8))
        shape = cell.cell.identity.shape
        assert background.summary.peak_student_in_flight == SPEC.conversations[shape]
        # Each background seat was still sending as the trial ended, and none sent after it.
        for student in range(1, 8):
            sent = [one.sent_ms for one in background.requests if one.student == student]
            assert max(sent) <= record.ended_ms
            assert max(sent) >= record.ended_ms - 2 * ANSWER_MS - SHORT["tool_delay_ms"][1]

    shapes = {cell.cell.identity.shape for cell in ran.cells}
    assert len(shapes) == 2
    # What each attempt recorded: a pass, its turns, retries and output, its whole duration with
    # its tool waits, and the most requests it had in flight at once.
    for cell in ran.cells:
        (executed,) = cell.trials
        record = executed.record
        assert record.outcome is TrialOutcome.PASSED
        assert (record.model_turns, record.retries, executed.output_tokens) == (3, 1, 900)
        assert executed.duration_ms == TRIAL_MS
        assert executed.side_calls == 1
        expected = 1 if family(CLASS.workload.trials[record.trial]) is Family.ANALYSIS else 2
        assert executed.peak_fan_out == expected
    assert (ran.successes, ran.attempts, ran.incomplete) == (4, 4, ())
    # Each attempt was released before the next started, and nothing is left sending.
    assert (harnesses.most_open, harnesses.open, harnesses.released) == (1, 0, 4)
    assert list(tmp_path.iterdir()) == []
    assert sum(pool.open.values()) == 0


def test_each_attempt_has_its_background_drained_before_the_next_starts(tmp_path: Path) -> None:
    chosen = cells(TWICE, Family.OPTIMIZATION, Family.RESEARCH, phase=Phase.LIVE_CLASS_LOAD)
    ran, _pool, _harnesses = run(TWICE, chosen, tmp_path)
    assert ran is not None

    for cell in ran.cells:
        first, second = cell.trials
        assert first.background is not None and second.background is not None
        # The first attempt's background sent until it ended, and nothing of it is open after.
        assert max(one.sent_ms for one in first.background.requests) >= first.record.ended_ms - 3 * ANSWER_MS
        assert all(one.ended_ms <= second.record.started_ms for one in first.background.requests)
        assert second.background.record.started_ms <= second.record.started_ms
        assert {one.student for one in second.background.requests} == set(range(1, 8))
        # The cell's one record spans both attempts.
        record = cell.background.record
        assert record is not None and record.valid_for_qualification
        assert record.started_ms <= first.record.started_ms and second.record.ended_ms <= record.ended_ms
        assert len(cell.background.requests) == len(first.background.requests) + len(second.background.requests)
    assert (ran.attempts, ran.incomplete) == (2 * len(chosen), ())


def test_with_one_seat_each_trial_runs_once_and_counts_for_both_loads(tmp_path: Path) -> None:
    chosen = trials.live_cells(ALONE)
    ran, pool, harnesses = run(ALONE, chosen, tmp_path)
    assert ran is not None

    assert {cell.cell.identity.phase for cell in ran.cells} == {Phase.LIVE_LOW_LOAD}
    assert sorted(record.trial for record in ran.records) == list(range(len(ALONE.workload.trials)))
    assert all(one.loads == plan.LOADS for one in ran.executed)
    assert ALONE.trials_by_load == dict.fromkeys(plan.LOADS, len(ALONE.workload.trials))
    assert pool.arrived == []
    assert harnesses.released == len(ALONE.workload.trials)


@pytest.mark.parametrize(
    ("act", "failure"),
    [
        (Act(answer="wrong"), FailureReason.REJECTED),
        (Act(failure=FailureReason.TIMEOUT), FailureReason.TIMEOUT),
        (Act(failure=FailureReason.ENDPOINT_FAILURE), FailureReason.ENDPOINT_FAILURE),
    ],
    ids=["wrong-artifact", "timeout", "endpoint-refusal"],
)
def test_a_wrong_artifact_a_timeout_or_an_endpoint_refusal_is_a_failed_attempt(
    act: Act, failure: FailureReason, tmp_path: Path
) -> None:
    chosen = cells(CLASS, Family.OPTIMIZATION, phase=Phase.LIVE_CLASS_LOAD)
    ran, _pool, _harnesses = run(CLASS, chosen, tmp_path, acts={Family.OPTIMIZATION: act})
    assert ran is not None

    (record,) = ran.records
    assert (record.outcome, record.failure, record.invalid) == (TrialOutcome.FAILED, failure, None)
    assert record.counts_as_attempt
    # A rejection keeps the grade that rejected it.
    assert (record.grade is not None) == (failure is FailureReason.REJECTED)
    assert (ran.successes, ran.attempts, ran.incomplete) == (0, 1, ())


def short(trial: TrialIdentity, work: trials.Work) -> Grade:
    """A grader that leaves the task's last required property unanswered."""
    return Grade(properties=(True,) * (trials.task_of(CLASS, trial).required_properties - 1), within_limits=True)


@pytest.mark.parametrize("grader", [broken, short], ids=["raises", "answers-too-few-properties"])
def test_a_broken_grader_leaves_the_attempt_invalid_and_its_cell_incomplete(
    grader: trials.Grader, tmp_path: Path
) -> None:
    chosen = cells(CLASS, Family.ANALYSIS, phase=Phase.LIVE_CLASS_LOAD)
    ran, _pool, harnesses = run(CLASS, chosen, tmp_path, graders=GRADERS | {Family.ANALYSIS: grader})
    assert ran is not None

    (record,) = ran.records
    assert (record.outcome, record.invalid, record.grade) == (TrialOutcome.INVALID, InvalidReason.GRADER_ERROR, None)
    assert not record.counts_as_attempt
    assert (ran.attempts, ran.incomplete) == (0, tuple(chosen))
    assert harnesses.released == 1


def test_a_broken_replay_stops_the_attempt_it_loads_and_leaves_it_invalid(tmp_path: Path) -> None:
    chosen = cells(CLASS, Family.OPTIMIZATION, phase=Phase.LIVE_CLASS_LOAD)
    ran, _pool, harnesses = run(CLASS, chosen, tmp_path, breaks_at=1_000)
    assert ran is not None

    (executed,) = ran.executed
    assert (executed.record.outcome, executed.record.invalid) == (TrialOutcome.INVALID, InvalidReason.DRIVER_ERROR)
    assert executed.background is None
    # The live task did not go on alone.
    assert executed.duration_ms < TRIAL_MS
    assert (ran.attempts, ran.incomplete) == (0, tuple(chosen))
    assert (harnesses.open, harnesses.released) == (0, 1)


@pytest.mark.parametrize("how", ["event", "task"])
def test_cancellation_stops_all_background_work_and_releases_the_attempt(how: str, tmp_path: Path) -> None:
    chosen = cells(CLASS, Family.OPTIMIZATION, Family.ANALYSIS, phase=Phase.LIVE_CLASS_LOAD)
    at = START + TRIAL_MS // 2
    if how == "event":
        ran, pool, harnesses = run(CLASS, chosen, tmp_path, cancel_at=at)
    else:
        ran, pool, harnesses = run(CLASS, chosen, tmp_path, task_cancelled_at=at)

    # The background was sending until then, and sent nothing after; no stream is left open.
    assert any(sent >= at - 2 * ANSWER_MS for sent, _student in pool.arrived)
    assert all(sent <= at for sent, _student in pool.arrived)
    assert sum(pool.open.values()) == 0
    # The attempt was released, and no other was started.
    assert (harnesses.open, harnesses.released) == (0, 1)
    assert list(tmp_path.iterdir()) == []
    if ran is not None:
        (cell,) = ran.cells
        (executed,) = cell.trials
        assert ran.cancelled
        assert (executed.record.outcome, executed.record.invalid) == (TrialOutcome.INVALID, InvalidReason.INTERRUPTED)
        assert ran.incomplete == (cell.cell.identity.cell,)
        assert executed.background is not None
        assert executed.background.summary.stopped_by is replay.Stop.CANCELLED


def test_a_cancelled_run_sends_nothing_more_while_its_attempt_is_released(tmp_path: Path) -> None:
    clock = Clock()
    pool = Gateway(clock, KEYS)
    gateway = replay.Gateway("https://gateway.example.com", "coursework", clock, pool)
    inner = Harnesses(clock, tmp_path)
    releasing, released = asyncio.Event(), asyncio.Event()

    class SlowRelease:
        @contextlib.asynccontextmanager
        async def attempt(self, trial: TrialIdentity, key: str, cancel: asyncio.Event) -> AsyncGenerator[trials.Work]:
            try:
                async with inner.attempt(trial, key, cancel) as work:
                    yield work
            finally:
                releasing.set()
                await released.wait()

    async def exercise() -> None:
        chosen = cells(CLASS, Family.OPTIMIZATION, phase=Phase.LIVE_CLASS_LOAD)
        task = asyncio.ensure_future(trials.run(CLASS, gateway, KEYS, SlowRelease(), GRADERS, cells=chosen))
        await clock.sleep_ms(TRIAL_MS // 2)
        assert pool.arrived
        task.cancel()
        await releasing.wait()
        arrived = len(pool.arrived)
        await clock.sleep_ms(5_000)
        # Nothing more reached the gateway, and no stream is left open, while the worker is released.
        assert len(pool.arrived) == arrived
        assert sum(pool.open.values()) == 0
        assert not task.done()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    clock.drive(exercise())


def test_a_driver_lagging_past_its_tolerance_leaves_the_loaded_cell_incomplete(tmp_path: Path) -> None:
    from evaluate.tests.test_replay import Busy

    clock = Clock()
    tolerance = CLASS.spec.protocol["driver"]["scheduling_lag_p95_ms_max"]
    lagging = Busy(clock, late_ms=tolerance + 50)  # ty: ignore[invalid-argument-type] - the replay's clock double
    gateway = replay.Gateway("https://gateway.example.com", "coursework", lagging, Gateway(clock, KEYS))
    chosen = cells(CLASS, Family.OPTIMIZATION, phase=Phase.LIVE_CLASS_LOAD)
    ran = clock.drive(trials.run(CLASS, gateway, KEYS, Harnesses(clock, tmp_path), GRADERS, cells=chosen))

    (cell,) = ran.cells
    record = cell.background.record
    assert record is not None and record.driver_lag_p95_ms is not None
    assert record.driver_lag_p95_ms > tolerance and not record.valid_for_qualification
    # The trial is kept as it ran, and its cell does not count.
    assert [one.outcome for one in ran.records] == [TrialOutcome.PASSED]
    assert ran.incomplete == tuple(chosen)


def test_each_attempt_replays_background_conversations_of_its_own(tmp_path: Path) -> None:
    from evaluate.tests.test_replay import Pool

    clock = Clock()
    pool = Pool(clock, KEYS)  # ty: ignore[invalid-argument-type] - the replay test's clock double
    gateway = replay.Gateway("https://gateway.example.com", "coursework", clock, pool)
    chosen = cells(TWICE, Family.OPTIMIZATION, phase=Phase.LIVE_CLASS_LOAD)
    ran = clock.drive(trials.run(TWICE, gateway, KEYS, Harnesses(clock, tmp_path), GRADERS, cells=chosen))

    (cell,) = ran.cells
    assert len(cell.trials) == 2
    # Each background conversation opened with a transcript of its own, under the cell's one prefix.
    openings = [one for one in pool.arrived if one.turn == 0]
    assert len(openings) > 2 * 7
    assert len({one.content for one in openings}) == len(openings)
    assert len({one.prefix for one in openings}) == 1
    numbers = [{one.conversation for one in attempt.background.requests} for attempt in cell.trials if attempt.background]
    assert len(numbers) == 2 and not numbers[0] & numbers[1]
    assert ran.incomplete == ()


def test_a_forwarder_that_cannot_listen_leaves_an_invalid_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evaluate import forwarding

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("no local listener available")

    monkeypatch.setattr(forwarding.ThreadingHTTPServer, "server_bind", unavailable)

    def never(_spec: workers.Spec) -> Worker:
        raise AssertionError("no worker is started without a forwarder")

    clock = replay.RunClock()
    harnesses = worker_harnesses(ALONE, never, lambda _trial: trials.Task(tmp_path, ("Run",)), clock)
    gateway = replay.Gateway("https://gateway.example.com", "coursework", clock)
    chosen = cells(ALONE, Family.OPTIMIZATION)
    ran = asyncio.run(trials.run(ALONE, gateway, KEYS, harnesses, GRADERS, cells=chosen))

    assert {record.invalid for record in ran.records} == {InvalidReason.DRIVER_ERROR}
    assert (ran.attempts, ran.incomplete) == (0, tuple(chosen))


def test_peak_fan_out_is_requests_in_flight_at_once() -> None:
    def peak(*spans: tuple[int, int]) -> int:
        return trials.peak_fan_out(evidence(0, 10_000, Act(spans=spans)))

    assert peak((0, 10), (10, 20)) == 1
    assert peak((0, 10), (5, 15), (6, 7)) == 3
    assert peak((0, 10), (5, 15), (12, 20)) == 2
    # A request the clock saw end in the millisecond it was sent was still in flight.
    assert peak((0, 0)) == 1
    assert peak((0, 10), (10, 10)) == 1
    assert peak((0, 10), (5, 5)) == 2


# --- The harnesses of a real run -------------------------------------------------------------


@dataclass
class Worker:
    """A worker double: the harness's command leaves an answer in its working folder."""

    workspace: Path
    commands: list[list[str]] = field(default_factory=list)
    discarded: bool = False
    transcript: Path = field(init=False)

    def __post_init__(self) -> None:
        self.transcript = self.workspace / "transcript"

    def run(self, command: Sequence[str]) -> workers.Ran:
        self.commands.append(list(command))
        (self.workspace / "work").mkdir(exist_ok=True)
        (self.workspace / "work" / "answer.txt").write_text("right", encoding="utf-8")
        return workers.Ran(0, False, False, self.transcript)

    def collect(self, folder: str = workers.OUTPUT) -> Path:
        collected = self.workspace / Path(folder).name
        collected.mkdir(exist_ok=True)
        return collected

    def stop(self) -> None:
        pass

    def discard(self) -> list[str]:
        shutil.rmtree(self.workspace)
        self.discarded = True
        return []


LIMITS = workers.Limits(60, 2**30, 1000, 64, 2**20, 2**20)


def worker_harnesses(
    planned: plan.Plan,
    start: Callable[[workers.Spec], trials.TrialWorker],
    tasks: Callable[[TrialIdentity], trials.Task],
    clock: replay.RunClock,
    *,
    image: str = "trial-image",
    limits: workers.Limits = LIMITS,
) -> trials.WorkerHarnesses:
    return trials.WorkerHarnesses(
        planned,
        upstream="https://gateway.example.com",
        pool="coursework",
        image=image,
        limits=limits,
        start=start,
        tasks=tasks,
        clock=clock.now_ms,
    )


def one_worker(tmp_path: Path, kind: type[Worker] = Worker) -> Worker:
    worker = kind(tmp_path / "worker")
    worker.workspace.mkdir()
    worker.transcript.touch()
    return worker


def test_each_attempt_runs_its_harness_adapter_in_a_fresh_worker_released_after_grading(tmp_path: Path) -> None:
    started: list[tuple[workers.Spec, Worker]] = []
    graded: list[bool] = []

    def start(spec: workers.Spec) -> Worker:
        worker = Worker(tmp_path / f"worker-{len(started)}")
        worker.workspace.mkdir()
        worker.transcript.touch()
        started.append((spec, worker))
        return worker

    def grade_in_place(trial: TrialIdentity, work: trials.Work) -> Grade:
        # The attempt's worker is the last started, and the one before it is gone already.
        graded.append(not started[-1][1].discarded and all(worker.discarded for _spec, worker in started[:-1]))
        return grade(trial, work)

    clock = replay.RunClock()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    harnesses = worker_harnesses(ALONE, start, lambda _trial: trials.Task(inputs, ("Do the task in TASK.md.",)), clock)
    gateway = replay.Gateway("https://gateway.example.com", "coursework", clock, Gateway(Clock(), KEYS))
    chosen = cells(ALONE, Family.OPTIMIZATION, Family.ANALYSIS)
    ran = asyncio.run(trials.run(ALONE, gateway, KEYS, harnesses, dict.fromkeys(Family, grade_in_place), cells=chosen))

    assert [record.outcome for record in ran.records] == [TrialOutcome.PASSED] * len(chosen)
    # One fresh worker an attempt, reaching only its own forwarder, given the prompt by the adapter.
    assert len(started) == len(chosen) == len(graded)
    for spec, worker in started:
        assert spec.forwarder is not None and spec.forwarder[0] == "127.0.0.1"
        assert spec.inputs == inputs
        (command,) = worker.commands
        assert command[:3] == ["python", "-I", "-c"]
        assert json.loads(command[-1])["argv"][:3] == ["claude", "-p", "Do the task in TASK.md."]
    # Graded before its worker was discarded, and every worker discarded after.
    assert all(graded)
    assert all(worker.discarded and not worker.workspace.exists() for _spec, worker in started)


def test_what_a_task_writes_in_its_working_folder_reaches_its_grader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evaluate.tests.test_workers import IMAGE, Runtime
    from evaluate.tests.test_workers import LIMITS as RUNTIME_LIMITS

    cell = cells(ALONE, Family.OPTIMIZATION)[0]
    identity = ALONE.workload.trials[ALONE.cells[cell].trials[0]]
    inputs, root = tmp_path / "inputs", tmp_path / "trial"
    optimization.materialize(identity.instance_id, inputs)
    # The worker's folders are folders of this host, and its commands run here, in them.
    work = root / Path(workers.WORK).name
    shutil.copytree(inputs, work)
    (root / Path(workers.OUTPUT).name).mkdir()
    reference = optimization.reference_artifacts(identity.instance_id)
    harness = tmp_path / "bin" / "claude"
    harness.parent.mkdir()
    harness.write_text(f"#!/bin/sh\ncp '{reference}/plan.json' '{reference}/plan.tex' .\n", encoding="utf-8")
    harness.chmod(0o755)
    monkeypatch.setenv("PATH", f"{harness.parent}:{sys.exec_prefix}/bin:/usr/bin:/bin")

    class Here(Runtime):
        def stream(self, arguments: Sequence[str], *, sink: Path, limit: int, seconds: float) -> workers.Streamed:
            command = [one.replace("/trial/", f"{root}/") for one in arguments[2:]]
            with sink.open("ab") as file:
                done = subprocess.run(command, cwd=work, stdout=file, timeout=seconds, check=False)
            return workers.Streamed(done.returncode, False)

    clock = replay.RunClock()
    gateway = replay.Gateway("https://gateway.example.com", "coursework", clock)
    with workers.Workers(tmp_path / "scratch", runtime=Here()) as started:
        harnesses = worker_harnesses(
            ALONE,
            started.start,
            lambda _trial: trials.Task(inputs, ("Solve it.",)),
            clock,
            image=IMAGE,
            limits=RUNTIME_LIMITS,
        )
        ran = asyncio.run(trials.run(ALONE, gateway, KEYS, harnesses, trials.course_graders(), cells=[cell]))

    (record,) = ran.records
    assert record.outcome is TrialOutcome.PASSED
    assert record.grade is not None and record.grade.accepted


def test_a_task_that_cannot_be_prepared_is_an_invalid_attempt(tmp_path: Path) -> None:
    def tasks(_trial: TrialIdentity) -> trials.Task:
        raise FileNotFoundError("the task's fixture is missing")

    clock = replay.RunClock()
    def never(_spec: workers.Spec) -> Worker:
        raise AssertionError("no worker is started for a task that cannot be prepared")

    harnesses = worker_harnesses(ALONE, never, tasks, clock)
    chosen = cells(ALONE, Family.OPTIMIZATION)
    gateway = replay.Gateway("https://gateway.example.com", "coursework", clock)
    ran = asyncio.run(trials.run(ALONE, gateway, KEYS, harnesses, GRADERS, cells=chosen))

    assert {record.invalid for record in ran.records} == {InvalidReason.DRIVER_ERROR}
    assert (ran.attempts, ran.incomplete) == (0, tuple(chosen))


def test_the_trial_deadline_stops_a_worker_waiting_on_a_tool(tmp_path: Path) -> None:
    wall_ms = 1_500
    planned = plan.expand(replace(SPEC, limits=replace(SPEC.limits, trial_wall_time_ms=wall_ms)), Profile.QUALIFY, 1)
    stopped = threading.Event()

    class Waiting(Worker):
        def run(self, command: Sequence[str]) -> workers.Ran:
            # Its tool waits far past the trial's wall time, unless the worker is ended.
            ended = stopped.wait(30)
            return workers.Ran(137 if ended else 0, False, False, self.transcript)

        def stop(self) -> None:
            stopped.set()

    worker = one_worker(tmp_path, Waiting)

    def start(_spec: workers.Spec) -> Worker:
        time.sleep(0.5)
        return worker

    clock = replay.RunClock()
    harnesses = worker_harnesses(planned, start, lambda _trial: trials.Task(tmp_path, ("Run",)), clock)
    gateway = replay.Gateway("https://gateway.example.com", "coursework", clock)
    chosen = cells(planned, Family.OPTIMIZATION)[:1]
    ran = asyncio.run(trials.run(planned, gateway, KEYS, harnesses, GRADERS, cells=chosen, evidence=None))

    (executed,) = ran.executed[:1]
    assert (executed.record.outcome, executed.record.failure) == (TrialOutcome.FAILED, FailureReason.TIMEOUT)
    assert executed.record.counts_as_attempt
    # The worker's start counted, and the wait was cut at the trial's wall time.
    assert wall_ms <= executed.duration_ms < wall_ms + 1_000
    assert stopped.is_set() and worker.discarded


def test_cancelling_while_a_worker_starts_launches_no_harness_in_it(tmp_path: Path) -> None:
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    launched: list[list[str]] = []

    class Stoppable(Worker):
        def run(self, command: Sequence[str]) -> workers.Ran:
            launched.append(list(command))
            return super().run(command)

        def stop(self) -> None:
            stopped.set()

    worker = one_worker(tmp_path, Stoppable)

    def start(_spec: workers.Spec) -> Worker:
        entered.set()
        release.wait(5)
        return worker

    clock = replay.RunClock()
    harnesses = worker_harnesses(ALONE, start, lambda _trial: trials.Task(tmp_path, ("Run",)), clock)

    async def exercise() -> None:
        async def attempt() -> None:
            async with harnesses.attempt(ALONE.workload.trials[0], KEYS[0], asyncio.Event()):
                pass

        pending = asyncio.ensure_future(attempt())
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(exercise())
    assert launched == []
    assert stopped.is_set() and worker.discarded


def test_a_cancelled_run_returns_only_once_its_worker_is_disposed_of(tmp_path: Path) -> None:
    disposing, release = threading.Event(), threading.Event()

    class Slow(Worker):
        def discard(self) -> list[str]:
            disposing.set()
            release.wait(5)
            return super().discard()

    worker = one_worker(tmp_path, Slow)
    clock = replay.RunClock()
    harnesses = worker_harnesses(ALONE, lambda _spec: worker, lambda _trial: trials.Task(tmp_path, ("Run",)), clock)

    async def exercise() -> None:
        gateway = replay.Gateway("https://gateway.example.com", "coursework", clock)
        chosen = cells(ALONE, Family.OPTIMIZATION)[:1]
        pending = asyncio.ensure_future(trials.run(ALONE, gateway, KEYS, harnesses, GRADERS, cells=chosen))
        assert await asyncio.to_thread(disposing.wait, 5)
        pending.cancel()
        done, _ = await asyncio.wait({pending}, timeout=0.3)
        assert not done
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert worker.discarded

    asyncio.run(exercise())


# --- The graders of the course ---------------------------------------------------------------


def run_here(scratch: Path) -> trials.Execute:
    """`trials.Execute` on this host: a fresh copy of the files, the command run in it with this
    Python, and what it wrote in its output folder."""
    runs = itertools.count()

    def execute(inputs: Path, command: Sequence[str]) -> Path | None:
        folder = scratch / f"run-{next(runs)}"
        shutil.copytree(inputs, folder / "work")
        (folder / "output").mkdir()
        argv = [
            sys.executable if one == "python" else one.replace(workers.OUTPUT, str(folder / "output"))
            for one in command
        ]
        subprocess.run(argv, cwd=folder / "work", capture_output=True, timeout=60, check=False)
        return folder / "output"

    return execute


def test_the_course_graders_grade_every_family_the_qualification_plans() -> None:
    planned = plan.expand(COURSE, Profile.QUALIFY, 8)
    assert {family(trial) for trial in planned.workload.trials} <= trials.course_graders().keys()


@pytest.mark.parametrize("graded", list(Family))
def test_the_course_graders_accept_a_reference_answer_and_reject_a_missing_one(graded: Family, tmp_path: Path) -> None:
    trial = next(one for one in CLASS.workload.trials if family(one) is graded)
    within = evidence(0, TRIAL_MS, Act())
    grader = trials.course_graders()[graded]
    workers_overlapped = Delegation(2, overlapped=True)
    execute = run_here(tmp_path / "runs")
    missing = tmp_path / "missing"
    missing.mkdir()
    if graded is Family.DEBUGGING:
        # The repaired program, as the trial left it in its working folder.
        answer = tmp_path / "submitted"
        answer.mkdir()
        shutil.copyfile(revision.reference_solver(trial.instance_id), answer / revision.SOLVER)
    else:
        course = {Family.OPTIMIZATION: optimization, Family.ANALYSIS: analysis, Family.RESEARCH: research}[graded]
        answer = course.reference_artifacts(trial.instance_id)

    assert grader(trial, trials.Work(within, answer, workers_overlapped, execute)).accepted
    assert not grader(trial, trials.Work(within, missing, workers_overlapped, execute)).accepted


def test_the_debugging_grader_rejects_the_broken_program_and_needs_a_fresh_worker(tmp_path: Path) -> None:
    trial = next(one for one in CLASS.workload.trials if family(one) is Family.DEBUGGING)
    given = tmp_path / "given"
    revision.materialize(trial.instance_id, given)
    within = evidence(0, TRIAL_MS, Act())
    grader = trials.course_graders()[Family.DEBUGGING]

    assert not grader(trial, trials.Work(within, given, None, run_here(tmp_path / "runs"))).accepted
    with pytest.raises(RuntimeError):
        grader(trial, trials.Work(within, given))
