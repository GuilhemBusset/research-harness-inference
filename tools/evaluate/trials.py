"""Runs and grades the live trials of a frozen plan, at low load and under class contention
(docs/design/agent-session-evaluation.md, "Workload and measurement protocol" and "Course quality
and the grader").

`run` takes the live cells of a `plan.Plan` in the plan's order and, in each, its planned trials
one after the other. Each attempt is a fresh agent on its harness (`Harnesses.attempt`), graded
after it ended by the independent grader of its task's family (`Grader`), outside the trial: the
grader reads what the attempt left and never runs in its worker. The program a debugging attempt
submitted is run again by its grader, in a fresh worker of its own (`Work.execute`).

**One live seat.** The live trial is always student 0, with the cohort's first key: the seat a
live cell reserves (`replay.reserved_seats`). Its background is `replay.run_cell` on the same cell,
which puts the replay on the seats after it and sends nothing under that key, so exactly one seat
is replaced by live work: an eight-seat class-load cell has seven background seats, each in the
cell's shape (one conversation a seat in hands-on, two in several-agents). A low-load cell has no
replay slot and sends nothing beside its trial. With one seat the plan has only low-load cells, each
carrying both loads (`plan.expand`): each of them is run once here, and its trials count for both.

**Each attempt has its background.** The background of an attempt starts before it (`run_cell`'s
`started`), keeps sending while the attempt runs, and is told the live work has finished only once
the attempt has ended; it then drains and is awaited, before the attempt is graded and before the
next one starts: no request of one attempt's background is open in the next. A cell's record
(`Background.record`) spans the backgrounds of its attempts, so that each trial lies inside it, as
`quality.read` requires. A background that breaks (its driver raises) stops the attempt it loads at
once: the attempt is invalid, `driver_error`, and its cell incomplete; so is an attempt whose task
could not be prepared. Whatever ends the run, an error or the cancellation of its task, the
background is cancelled and awaited before `run` returns or raises.

**Cancellation.** `cancel` stops everything at once: the background sends nothing more and cuts its
streams (`Stops.cancel`), the attempt under way is told to stop and is recorded invalid,
`interrupted`, and no further attempt is started. Cancelling the task that runs `run` does the same,
and it returns only once the attempt's worker is disposed of.

**What an attempt counts as** is the contract's (`forwarding.Evidence.trial_record`):

- *passed*: the attempt stayed within its limits and the grader accepted every required property;
- *failed*: the grader rejected what it left (a wrong artifact), or the attempt timed out, crossed
  a turn or token limit, made a call it could not go on from, or the endpoint refused it. It
  counts as an attempt and stays in the denominator. Only an attempt that may still pass is
  graded: a crossed limit fails whatever the grade would say;
- *invalid*: the driver or the grader broke (a grader that raises, gives no grade or answers other
  properties than its task requires is `grader_error`), or the run
  was cancelled during it. It does not count, and its cell is incomplete until it is replaced.

Every attempt is kept (`Executed`), with what the contract's record holds (outcome and reason,
turns across the agent tree, retries, usage, and the attempt's start and end on the run's clock:
its whole duration, tool waits included) and what it does not: the loads it counts for, its peak
fan-out, its side calls and its background. The **fan-out** is the most requests of the live seat
that were in flight at the gateway at once during the attempt (`peak_fan_out`), read from the
forwarder's measurements; the delegation the harness reported is kept beside it for the grader,
and is no count of concurrency.

**Release.** An attempt's worker, its forwarder and its workspace belong to the attempt
(`Harnesses.attempt` is a context manager): they are released when the attempt has been graded,
before the next one starts, also when grading or the run fails. `WorkerHarnesses` is the harness
runner of a real run: a fresh worker (`workers.Workers.start`) and a forwarder of its own
(`forwarding.Forwarder`, the plan's limits) for each attempt, the harness driven by its
`adapters.Adapter`, the attempt's ending read by `Ending.evidence`, and what the task wrote
collected from where it writes it (`Task.artifacts`). The plan's wall time governs the whole
attempt, the worker's start and its tool waits included: once it has passed, the worker is stopped
wherever it is and the attempt has timed out.

What is recorded is ordinals, times on the run's clock, counts and labels: no key, no host.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from common import percentile
from evaluate import replay, workers
from evaluate.adapters import Adapter
from evaluate.contracts import (
    LIVE_PHASES,
    CellRecord,
    CellStatus,
    Family,
    Grade,
    InvalidFlag,
    InvalidReason,
    TrialIdentity,
    TrialOutcome,
    TrialRecord,
)
from evaluate.course import analysis, optimization, research, revision
from evaluate.forwarding import CANCEL_S, Budget, Evidence, Forwarder
from evaluate.streams import Attempts

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from evaluate.adapters import Delegation
    from evaluate.contracts import HarnessAssignment, RequestRecord
    from evaluate.contracts import Task as SuiteTask
    from evaluate.plan import Plan, PlannedCell, Role

# The student a live trial runs as: the seat `replay.reserved_seats` keeps, the first.
LIVE_STUDENT = 0

type Execute = Callable[[Path, Sequence[str]], Path | None]
"""Runs one command in a fresh worker, with no way out, given the files of a folder, and gives
what it left in its output folder; `None` when the command did not end within the worker's wall
time or left more than its cap. WorkerFault when the worker could not run it."""


@dataclass(frozen=True)
class Work:
    """What an attempt left for its grader: the trial's evidence (`forwarding.Evidence`), the
    folder of what it produced, the delegation its harness reported, if it read one, and how a
    program it submitted is run again, where the runner can."""

    evidence: Evidence
    artifacts: Path
    delegation: Delegation | None = None
    execute: Execute | None = None


class Harnesses(Protocol):
    """The harness runner: one fresh attempt of a planned trial at a time."""

    def attempt(self, trial: TrialIdentity, key: str, cancel: asyncio.Event) -> AbstractAsyncContextManager[Work]:
        """Runs one attempt of `trial` as the student whose key is `key`, to its end or until
        `cancel` is set, and gives what it left. Everything the attempt holds (its worker, its
        forwarder, what it produced) is released when the context ends."""
        ...


Grader = Callable[[TrialIdentity, Work], Grade]
"""The independent grader of a family: a grade of what an attempt left. One that raises could not
grade, and the attempt is invalid."""


def peak_fan_out(evidence: Evidence) -> int:
    """The most of the trial's requests that were in flight at the gateway at once: those that the
    forwarder sent on, each from its dispatch to its end. A request that ends when the next starts
    does not overlap it; one whose end the clock does not tell from its dispatch was in flight."""
    edges = []
    for one in evidence.requests:
        sent, ended = one.attempt.sent_ms, one.attempt.ended_ms
        edges.append((sent, 1, 1))
        # At one instant, what ended before it is let go first, then what starts, then what
        # started and ended within it.
        edges.append((ended, 0, -1) if ended > sent else (sent, 2, -1))
    peak = held = 0
    for _at, _order, change in sorted(edges):
        held += change
        peak = max(peak, held)
    return peak


@dataclass(frozen=True)
class Executed:
    """One attempt as it was recorded: the contract's record, and what the record does not hold.
    `background` is the replay that loaded it; `None` when its driver broke, or never started."""

    record: TrialRecord
    loads: tuple[Role, ...]
    peak_fan_out: int
    side_calls: int
    delegation: Delegation | None
    background: replay.CellRun | None

    @property
    def duration_ms(self) -> int:
        """The task's wall time, tool waits included: from the attempt's start to its end."""
        return self.record.ended_ms - self.record.started_ms

    @property
    def output_tokens(self) -> int | None:
        usage = self.record.usage
        return None if usage is None else usage.output_tokens


@dataclass(frozen=True)
class Background:
    """A live cell's background over its attempts: each attempt's replay, every request they sent
    in order, and the cell's one record, which spans them (`None` when none ran)."""

    runs: tuple[replay.CellRun, ...]
    requests: tuple[RequestRecord, ...]
    record: CellRecord | None


def _cell_record(cell: int, runs: Sequence[replay.CellRun], *, whole: bool) -> CellRecord | None:
    """The record of a cell whose attempts each had a run: from the first's start to the last's
    end, its conversations summed, its flags joined and its driver's lag read over every request.
    Incomplete unless `whole`: every planned attempt ran, each with its background."""
    if not runs:
        return None
    records = [one.record for one in runs]
    lags = [request.sent_ms - request.eligible_ms for one in runs for request in one.requests]
    lag = percentile(lags, 95) if lags else None
    flags = {flag for record in records for flag in record.invalid_flags}
    if lag is not None and lag > runs[0].summary.lag_tolerance_ms:
        flags.add(InvalidFlag.DRIVER_SATURATION)
    complete = whole and all(record.status is CellStatus.COMPLETE for record in records)
    return CellRecord(
        cell=cell,
        started_ms=records[0].started_ms,
        admissions_ended_ms=records[-1].admissions_ended_ms,
        ended_ms=records[-1].ended_ms,
        status=CellStatus.COMPLETE if complete else CellStatus.INCOMPLETE,
        invalid_flags=tuple(sorted(flags)),
        conversations_planned=sum(record.conversations_planned for record in records),
        conversations_started=sum(record.conversations_started for record in records),
        conversations_completed=sum(record.conversations_completed for record in records),
        conversations_abandoned=sum(record.conversations_abandoned for record in records),
        hosts_observed=None,
        driver_lag_p95_ms=lag,
    )


@dataclass(frozen=True)
class CellRan:
    """A live cell: its attempts, in order, each with its background."""

    cell: PlannedCell
    trials: tuple[Executed, ...]

    @property
    def whole(self) -> bool:
        """Whether every planned attempt ran, each loaded by its background to its end."""
        ran = len(self.trials) == len(self.cell.trials)
        return ran and all(one.background is not None for one in self.trials)

    @property
    def background(self) -> Background:
        runs = tuple(one.background for one in self.trials if one.background is not None)
        requests = tuple(request for one in runs for request in one.requests)
        return Background(runs, requests, _cell_record(self.cell.identity.cell, runs, whole=self.whole))

    @property
    def background_students(self) -> frozenset[int]:
        """The students the background sent as."""
        return frozenset(one.student for one in self.background.requests)

    @property
    def complete(self) -> bool:
        """Whether every planned trial of the cell has an attempt that counts, each with its whole
        background, and the background measured what the plan asked of it (`valid_for_qualification`:
        a driver that lagged past its tolerance, or a cell short of its evidence, does not count):
        an invalid attempt leaves it incomplete until it is replaced."""
        counted = {one.record.trial for one in self.trials if one.record.counts_as_attempt}
        record = self.background.record
        loaded = record is not None and record.valid_for_qualification
        return loaded and counted == set(self.cell.trials)


@dataclass(frozen=True)
class Run:
    """The live cells that were run, in order, and whether the run was cancelled."""

    cells: tuple[CellRan, ...]
    cancelled: bool

    @property
    def executed(self) -> tuple[Executed, ...]:
        return tuple(one for cell in self.cells for one in cell.trials)

    @property
    def records(self) -> tuple[TrialRecord, ...]:
        """Every attempt's record, failed and invalid ones included."""
        return tuple(one.record for one in self.executed)

    @property
    def attempts(self) -> int:
        """The attempts that count: passes and completed failures."""
        return sum(record.counts_as_attempt for record in self.records)

    @property
    def successes(self) -> int:
        return sum(record.outcome is TrialOutcome.PASSED for record in self.records)

    @property
    def incomplete(self) -> tuple[int, ...]:
        """The cells left incomplete."""
        return tuple(cell.cell.identity.cell for cell in self.cells if not cell.complete)


def _family(trial: TrialIdentity) -> Family:
    return Family(trial.instance_id.split(".")[0])


def task_of(plan: Plan, trial: TrialIdentity) -> SuiteTask:
    """The task of the suite whose instance the trial runs."""
    return next(
        task
        for task in plan.spec.suite.tasks
        if any(instance.instance_id == trial.instance_id for instance in task.instances)
    )


def live_cells(plan: Plan) -> tuple[int, ...]:
    """The plan's live cells, in its order."""
    return tuple(cell.identity.cell for cell in plan.cells if cell.identity.phase in LIVE_PHASES)


async def _settled[Result](awaitable: Awaitable[Result]) -> Result:
    """Awaits `awaitable` to its end, also when the task waiting for it is cancelled meanwhile: the
    cancellation is raised once it has ended."""
    future = asyncio.ensure_future(awaitable)
    interrupted = False
    while not future.done():
        try:
            await asyncio.wait({future})
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return future.result()


async def _first(*awaitables: Awaitable[object]) -> None:
    """Waits until one of them has ended; the others that are left are cancelled and awaited."""
    futures = [asyncio.ensure_future(one) for one in awaitables]
    try:
        await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for one in futures:
            one.cancel()
        await _settled(asyncio.gather(*futures, return_exceptions=True))


class _Runner:
    def __init__(
        self,
        plan: Plan,
        gateway: replay.Gateway,
        keys: Sequence[str],
        harnesses: Harnesses,
        graders: Mapping[Family, Grader],
        cancel: asyncio.Event,
        evidence: replay.Evidence,
        execution: int,
        request: int,
    ) -> None:
        self.plan, self.gateway, self.keys, self.harnesses, self.graders = plan, gateway, keys, harnesses, graders
        self.cancel, self.evidence, self.execution, self.request = cancel, evidence, execution, request

    async def cell(self, cell: int) -> CellRan:
        planned = self.plan.cells[cell]
        executed: list[Executed] = []
        # The number of the next background conversation: each attempt's replay goes on from the
        # last one the attempt before it sent, so that no conversation's transcript is sent twice.
        conversation = 0
        for trial in planned.trials:
            if self.cancel.is_set():
                break
            one = await self.attempt(planned, self.plan.workload.trials[trial], conversation)
            executed.append(one)
            if one.background is None:
                # A replay that broke ends its cell, as a cell ends at a request it could not
                # record: what it sent is not known, nor which conversation could come next.
                break
            numbers = [request.conversation for request in one.background.requests]
            conversation = max(numbers, default=conversation - 1) + 1
        return CellRan(planned, tuple(executed))

    async def attempt(self, cell: PlannedCell, trial: TrialIdentity, conversation: int) -> Executed:
        """One attempt in its own background: started before it, sending until it has ended,
        drained before it is graded. When the task running it is cancelled, the background stops
        sending and cuts its streams at once, while the attempt's worker is released."""
        finished, started, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        background = asyncio.ensure_future(
            replay.run_cell(
                self.plan,
                cell.identity.cell,
                self.gateway,
                self.keys,
                # The run's own cancellation, which the replay reads at the instant of each
                # dispatch: once it is set, no seat sends anything more.
                stops=replay.Stops(finished=finished, cancel=self.cancel),
                first_request=self.request,
                evidence=self.evidence,
                started=started,
                first_conversation=conversation,
            )
        )
        # The attempt stops at once when the run is cancelled or its background ends before it.
        watch = asyncio.ensure_future(self._watch(background, stop))
        try:
            await _first(started.wait(), asyncio.shield(background))
            if not started.is_set():
                return self._unstarted(trial)
            live = asyncio.ensure_future(self._attempted(trial, background, finished, stop))
            try:
                return await asyncio.shield(live)
            except asyncio.CancelledError:
                # Nothing more is sent beside an attempt whose run was cancelled, while it unwinds:
                # its background is cancelled, which cuts every stream it has open.
                background.cancel()
                stop.set()
                live.cancel()
                await _settled(asyncio.wait({live}))
                raise
        finally:
            watch.cancel()
            background.cancel()
            await _settled(asyncio.gather(watch, background, return_exceptions=True))

    async def _watch(self, background: asyncio.Future[replay.CellRun], stop: asyncio.Event) -> None:
        await _first(asyncio.shield(background), self.cancel.wait())
        stop.set()

    async def _attempted(
        self,
        trial: TrialIdentity,
        background: asyncio.Future[replay.CellRun],
        finished: asyncio.Event,
        stop: asyncio.Event,
    ) -> Executed:
        async with self.harnesses.attempt(trial, self.keys[LIVE_STUDENT], stop) as work:
            # The trial has ended: only now does its background learn it, and drain.
            finished.set()
            ran: replay.CellRun | None = None
            try:
                ran = await background
            except Exception:  # noqa: BLE001 - a background that broke makes the attempt it loaded invalid
                ran = None
            evidence = work.evidence
            reason = (
                InvalidReason.DRIVER_ERROR
                if ran is None
                else InvalidReason.INTERRUPTED
                if self.cancel.is_set()
                else None
            )
            if reason is not None and evidence.invalid is None:
                evidence = replace(evidence, invalid=reason)
            grade: Grade | None = None
            if evidence.may_pass:
                try:
                    graded: object = self.graders[_family(trial)](trial, work)
                except Exception:  # noqa: BLE001 - a grader that could not grade makes the attempt invalid
                    graded = None
                required = task_of(self.plan, trial).required_properties
                if isinstance(graded, Grade) and len(graded.properties) == required:
                    grade = graded
                else:
                    # A grade answers each required property of its task: a grader that raised,
                    # gave no grade or answered other properties could not grade it.
                    evidence = replace(evidence, invalid=InvalidReason.GRADER_ERROR)
            record = evidence.trial_record(
                execution=self.execution, trial=trial.trial, student=LIVE_STUDENT, grade=grade
            )
        self.execution += 1
        if ran is not None:
            self.request += len(ran.requests)
        return Executed(
            record=record,
            loads=self.plan.loads_of(trial),
            peak_fan_out=peak_fan_out(evidence),
            side_calls=evidence.side_calls,
            delegation=work.delegation,
            background=ran,
        )

    def _unstarted(self, trial: TrialIdentity) -> Executed:
        """An attempt whose background broke before it started: no harness was run."""
        now = self.gateway.clock.now_ms()
        record = TrialRecord(
            execution=self.execution,
            trial=trial.trial,
            replaces=None,
            student=LIVE_STUDENT,
            started_ms=now,
            ended_ms=now,
            outcome=TrialOutcome.INVALID,
            failure=None,
            invalid=InvalidReason.INTERRUPTED if self.cancel.is_set() else InvalidReason.DRIVER_ERROR,
            grade=None,
            model_turns=0,
            retries=0,
            usage=None,
        )
        self.execution += 1
        return Executed(record, self.plan.loads_of(trial), 0, 0, None, None)


async def run(
    plan: Plan,
    gateway: replay.Gateway,
    keys: Sequence[str],
    harnesses: Harnesses,
    graders: Mapping[Family, Grader],
    *,
    cells: Sequence[int] | None = None,
    cancel: asyncio.Event | None = None,
    evidence: replay.Evidence | None = None,
    first_execution: int = 0,
    first_request: int = 0,
) -> Run:
    """Runs the live cells of the plan, all of them in order unless `cells` names some, and returns
    every attempt (the module's text). `keys` are the cohort's canary keys: the first is the live
    seat's. `evidence` is what earlier cells showed of each student's prefix cache. Executions are
    numbered from `first_execution`, background requests from `first_request`.

    ValueError, before anything is sent, for a cell that is not live or a family with no grader;
    `replay.ReplayError` for keys too few for the plan."""
    chosen = live_cells(plan) if cells is None else tuple(cells)
    if any(plan.cells[cell].identity.phase not in LIVE_PHASES for cell in chosen):
        raise ValueError("only a live cell has trials to run")
    families = {_family(plan.workload.trials[trial]) for cell in chosen for trial in plan.cells[cell].trials}
    if missing := sorted(families - set(graders)):
        raise ValueError(f"no grader for {', '.join(missing)}")
    cohort = replay.preflight(plan, keys)[0]
    cancel = asyncio.Event() if cancel is None else cancel
    runner = _Runner(
        plan,
        gateway,
        cohort,
        harnesses,
        graders,
        cancel,
        replay.Evidence() if evidence is None else evidence,
        first_execution,
        first_request,
    )
    ran: list[CellRan] = []
    for cell in chosen:
        if cancel.is_set():
            break
        ran.append(await runner.cell(cell))
    return Run(tuple(ran), cancel.is_set())


# --- The graders of the course ---------------------------------------------------------------

# Where the debugging grader puts the cases beside the submitted program, in a fresh worker.
GRADING_CASES = "grading-cases"


def _graded(properties: tuple[bool, ...]) -> Grade:
    return Grade(properties=properties, within_limits=True)


def _debugging(trial: TrialIdentity, work: Work) -> Grade:
    """The submitted program, run on each sealed case of its instance in a fresh worker, and what
    it wrote there graded against the cases (`course.revision`)."""
    if work.execute is None:
        raise RuntimeError("the submitted program is run in a fresh worker, and the runner gave none")
    with tempfile.TemporaryDirectory() as scratch:
        submitted, outputs = Path(scratch) / "submitted", Path(scratch) / "outputs"
        shutil.copytree(work.artifacts, submitted, symlinks=True)
        cases = submitted / GRADING_CASES
        if cases.is_dir() and not cases.is_symlink():
            shutil.rmtree(cases)
        else:
            cases.unlink(missing_ok=True)
        outputs.mkdir()
        for name in revision.write_inputs(trial.instance_id, cases):
            command = revision.command(f"{GRADING_CASES}/{name}.json", f"{workers.OUTPUT}/{name}.json")
            produced = work.execute(submitted, command)
            written = None if produced is None else produced / f"{name}.json"
            if written is not None and written.is_file() and not written.is_symlink():
                shutil.copyfile(written, outputs / f"{name}.json")
        return _graded(revision.grade(trial.instance_id, outputs).properties)


def course_graders() -> dict[Family, Grader]:
    """The course's graders, one for each family: optimization, parallel analysis (with the
    delegation its harness reported) and grounded research read what an attempt wrote; debugging
    runs the program it submitted on its cases, in a fresh worker (`Work.execute`), and reads what
    that wrote. A grade is asked for only of an attempt within its limits."""
    return {
        Family.OPTIMIZATION: lambda trial, work: _graded(
            optimization.grade(trial.instance_id, work.artifacts).properties
        ),
        Family.DEBUGGING: _debugging,
        Family.ANALYSIS: lambda trial, work: _graded(
            analysis.grade(trial.instance_id, work.artifacts, work.delegation).properties
        ),
        Family.RESEARCH: lambda trial, work: _graded(research.grade(trial.instance_id, work.artifacts).properties),
    }


# --- The harnesses of a real run -------------------------------------------------------------


class TrialWorker(Protocol):
    """What the runner needs of a worker (`workers.Worker`)."""

    transcript: Path

    def run(self, command: Sequence[str]) -> workers.Ran: ...

    def collect(self, folder: str = workers.OUTPUT) -> Path: ...

    def stop(self) -> None: ...

    def discard(self) -> list[str]: ...


@dataclass(frozen=True)
class Task:
    """What a trial is given: the folder of its task's files, its prompts in order (the first, then
    each follow-up), its local tools by name, and the folder of the worker its task writes its
    files in, which its grader reads: the working folder, where every task of the course writes."""

    inputs: Path
    prompts: tuple[str, ...]
    tools: Mapping[str, Path] = field(default_factory=dict)
    artifacts: str = workers.WORK


class _Held:
    """The workers of one attempt, and whether it is being stopped: a worker that is started once
    it is is stopped at once, and runs nothing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: list[TrialWorker] = []
        self.stopping = False

    def take(self, worker: TrialWorker) -> bool:
        """Records a started worker; whether it may run anything."""
        with self._lock:
            self._workers.append(worker)
            return not self.stopping

    def stop(self, forwarder: Forwarder) -> None:
        """Stops the attempt: its way out is closed and every worker of it ended."""
        forwarder.abort()
        with self._lock:
            self.stopping = True
            started = list(self._workers)
        for worker in started:
            with contextlib.suppress(workers.WorkerFault):
                worker.stop()

    def discard(self) -> list[str]:
        """Disposes of every worker of the attempt; what is left."""
        with self._lock:
            started = list(self._workers)
        return [thing for worker in started for thing in worker.discard()]


class WorkerHarnesses:
    """The harnesses of a real run. Each attempt has a fresh worker (`start`, such as
    `workers.Workers.start`) of `image` under `limits`, and a forwarder of its own to the gateway
    at `upstream`, on the pool `pool`, holding the student's key and the plan's limits on `clock`,
    the run's: the machine's (`replay.RunClock`), which the attempt's deadline is waited on. `tasks`
    gives what a trial is given."""

    def __init__(
        self,
        plan: Plan,
        *,
        upstream: str,
        pool: str,
        image: str,
        limits: workers.Limits,
        start: Callable[[workers.Spec], TrialWorker],
        tasks: Callable[[TrialIdentity], Task],
        clock: Callable[[], int],
    ) -> None:
        self.plan, self.upstream, self.pool, self.image, self.limits = plan, upstream, pool, image, limits
        self.start, self.tasks, self.clock = start, tasks, clock

    def assignment(self, trial: TrialIdentity) -> HarnessAssignment:
        """The trial's harness as its task assigns it, with the features the task needs."""
        return next(one for one in task_of(self.plan, trial).harnesses if one.harness is trial.harness)

    @contextlib.asynccontextmanager
    async def attempt(self, trial: TrialIdentity, key: str, cancel: asyncio.Event) -> AsyncGenerator[Work]:
        budget = Budget(self.plan.workload.limits, self.clock)
        try:
            forwarder = Forwarder(self.upstream, pool=self.pool, key=key, harness=trial.harness, budget=budget)
        except OSError:
            # Its forwarder could not listen: the attempt never ran, and the driver broke.
            forwarder = None
        if forwarder is None:
            with tempfile.TemporaryDirectory() as nothing:
                yield Work(self._unmeasured(budget), Path(nothing))
            return
        held = _Held()
        ran: asyncio.Future[Work] | None = None
        with forwarder, tempfile.TemporaryDirectory() as nothing:
            try:
                try:
                    assignment, task = self.assignment(trial), self.tasks(trial)
                except Exception:  # noqa: BLE001 - a task that cannot be prepared is the driver's error
                    forwarder.abort()
                    work = Work(forwarder.evidence(invalid=InvalidReason.DRIVER_ERROR), Path(nothing))
                else:
                    ran = asyncio.ensure_future(
                        asyncio.to_thread(self._run, assignment, task, forwarder, held, Path(nothing))
                    )
                    await _first(asyncio.shield(ran), cancel.wait(), self._deadline(budget))
                    if not ran.done():
                        await _settled(asyncio.to_thread(held.stop, forwarder))
                    work = await _settled(ran)
                yield replace(work, execute=lambda inputs, command: self._execute(held, inputs, command))
            finally:
                if ran is not None and not ran.done():
                    await _settled(asyncio.to_thread(held.stop, forwarder))
                    await _settled(asyncio.wait({ran}))
                left = await _settled(asyncio.to_thread(held.discard))
                if left:
                    error = workers.Undisposed("an attempt left something behind")
                    for thing in left:
                        error.add_note(f"not removed: {thing}")
                    raise error

    def _unmeasured(self, budget: Budget) -> Evidence:
        """The evidence of an attempt whose forwarder never served: nothing was sent, and the
        measurement is the driver's error."""
        return Evidence(
            started_ms=budget.started_ms,
            ended_ms=self.clock(),
            failure=None,
            invalid=InvalidReason.DRIVER_ERROR,
            model_turns=0,
            side_calls=0,
            retries=0,
            refusals=0,
            generated_tokens=0,
            open_reservations=0,
            tally=Attempts().tally(),
            requests=(),
        )

    async def _deadline(self, budget: Budget) -> None:
        """Until the trial's wall time has passed on the run's clock."""
        while (left := budget.deadline_ms - self.clock()) > 0:
            await asyncio.sleep((left + 1) / 1000)

    def _stopped(self, forwarder: Forwarder) -> Evidence:
        """The evidence of an attempt stopped before its harness ran: timed out when its wall time
        had passed, interrupted otherwise."""
        forwarder.abort()
        timed_out = forwarder.budget.failure is not None
        return forwarder.evidence(invalid=None if timed_out else InvalidReason.INTERRUPTED)

    def _run(self, assignment: HarnessAssignment, task: Task, forwarder: Forwarder, held: _Held, nothing: Path) -> Work:
        """One attempt, in a thread: the worker, the harness's prompts, then what it produced."""
        address = urllib.parse.urlsplit(forwarder.url)
        spec = workers.Spec(
            self.image,
            task.inputs,
            self.limits,
            forwarder=(address.hostname or "", address.port or 0),
            tools=dict(task.tools),
        )
        try:
            worker = self.start(spec)
        except (workers.WorkerFault, workers.Refused, OSError):
            forwarder.abort()
            return Work(forwarder.evidence(invalid=InvalidReason.DRIVER_ERROR), nothing)
        if not held.take(worker):
            # Stopped while it was starting: it is ended, and the harness never launched.
            with contextlib.suppress(workers.WorkerFault):
                worker.stop()
            return Work(self._stopped(forwarder), nothing)
        adapter = Adapter(assignment, model=self.pool, tools=tuple(task.tools))
        trial = adapter.trial(worker, task.prompts, turns=lambda: forwarder.budget.model_turns)
        # The trial is over: nothing more is admitted, and what is in flight is settled.
        forwarder.abort()
        forwarder.drained(CANCEL_S)
        evidence = trial.ending.evidence(forwarder)
        if held.stopping and evidence.may_pass:
            # Stopped from outside before it was over: what it left is not graded.
            evidence = replace(evidence, invalid=InvalidReason.INTERRUPTED)
        artifacts = nothing
        try:
            if evidence.may_pass:
                artifacts = worker.collect(task.artifacts)
            worker.stop()
        except workers.OverCap:
            # What it left does not fit the cap: none of it is kept, and the grader finds nothing.
            artifacts = nothing
        except (workers.WorkerFault, OSError):
            evidence = replace(evidence, invalid=evidence.invalid or InvalidReason.DRIVER_ERROR)
        return Work(evidence, artifacts, trial.delegation)

    def _execute(self, held: _Held, inputs: Path, command: Sequence[str]) -> Path | None:
        """`Execute` for the grader of this attempt: a fresh worker with no way out, released with
        the attempt."""
        worker = self.start(workers.Spec(self.image, inputs, self.limits))
        if not held.take(worker):
            raise workers.WorkerFault("the attempt is being stopped")
        ran = worker.run(command)
        if ran.timed_out:
            return None
        try:
            return worker.collect()
        except workers.OverCap:
            return None
        finally:
            worker.stop()
