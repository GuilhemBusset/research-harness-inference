"""Runs and grades the live trials of a frozen plan, at low load and under class contention
(docs/design/agent-session-evaluation.md, "Workload and measurement protocol" and "Course quality
and the grader").

`run` takes the live cells of a `plan.Plan` in the plan's order and, in each, its planned trials
one after the other. Each attempt is a fresh agent on its harness (`Harnesses.attempt`), graded
after it ended by the independent grader of its task's family (`Grader`), outside the trial: the
grader reads what the attempt left and never runs in its worker.

**One live seat.** The live trial is always student 0, with the cohort's first key: the seat a
live cell reserves (`replay.reserved_seats`). Its background is `replay.run_cell` on the same cell,
which puts the replay on the seats after it and sends nothing under that key, so exactly one seat
is replaced by live work: an eight-seat class-load cell has seven background seats, each in the
cell's shape (one conversation a seat in hands-on, two in several-agents). A low-load cell has no
replay slot and sends nothing beside its trial. With one seat the plan has only low-load cells, each
carrying both loads (`plan.expand`): each of them is run once here, and its trials count for both.

**The background outlasts each trial.** The cell starts before its first attempt (`run_cell`'s
`started`, its record's start), keeps sending while each attempt runs, and is told the live work
has finished only once the cell's last attempt has ended and been graded; it then drains and is
awaited. Each trial therefore lies inside its cell's record, which `quality.read` requires: a cell
is measured by one record. Whatever ends the run, an error or the cancellation of its task, the
background is cancelled and awaited before `run` returns or raises: no background request is left.

**Cancellation.** `cancel` stops everything at once: the background sends nothing more and cuts its
streams (`Stops.cancel`), the attempt under way is told to stop and is recorded invalid,
`interrupted`, and no further attempt is started. Cancelling the task that runs `run` does the same.

**What an attempt counts as** is the contract's (`forwarding.Evidence.trial_record`):

- *passed*: the attempt stayed within its limits and the grader accepted every required property;
- *failed*: the grader rejected what it left (a wrong artifact), or the attempt timed out, crossed
  a turn or token limit, made a call it could not go on from, or the endpoint refused it. It
  counts as an attempt and stays in the denominator. Only an attempt that may still pass is
  graded: a crossed limit fails whatever the grade would say;
- *invalid*: the driver or the grader broke (a grader that raises is `grader_error`), or the run
  was cancelled during it. It does not count, and its cell is incomplete until it is replaced.

Every attempt is kept (`Executed`), with what the contract's record holds (outcome and reason,
turns across the agent tree, retries, usage, and the attempt's start and end on the run's clock:
its whole duration, tool waits included) and what it does not: the loads it counts for, its peak
fan-out and its side calls. The **fan-out** is the most requests of the live seat that were in
flight at the gateway at once during the attempt (`peak_fan_out`), read from the forwarder's
measurements; the delegation the harness reported is kept beside it for the grader, and is no
count of concurrency.

**Release.** An attempt's worker, its forwarder and its workspace belong to the attempt
(`Harnesses.attempt` is a context manager): they are released when the attempt has been graded,
before the next one starts, also when grading or the run fails. `WorkerHarnesses` is the harness
runner of a real run: a fresh worker (`workers.Workers.start`) and a forwarder of its own
(`forwarding.Forwarder`, the plan's limits) for each attempt, the harness driven by its
`adapters.Adapter`, and the attempt's ending read by `Ending.evidence`.

What is recorded is ordinals, times on the run's clock, counts and labels: no key, no host.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from evaluate import replay, workers
from evaluate.adapters import Adapter
from evaluate.contracts import LIVE_PHASES, CellStatus, Family, Grade, InvalidReason, TrialIdentity, TrialOutcome
from evaluate.course import analysis, optimization, research
from evaluate.forwarding import CANCEL_S, Budget, Forwarder

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from evaluate.adapters import Delegation
    from evaluate.contracts import HarnessAssignment, TrialRecord
    from evaluate.forwarding import Evidence
    from evaluate.plan import Plan, PlannedCell, Role

# The student a live trial runs as: the seat `replay.reserved_seats` keeps, the first.
LIVE_STUDENT = 0


@dataclass(frozen=True)
class Work:
    """What an attempt left for its grader: the trial's evidence (`forwarding.Evidence`), the
    folder of what it produced, and the delegation its harness reported, if it read one."""

    evidence: Evidence
    artifacts: Path
    delegation: Delegation | None = None


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
    does not overlap it."""
    edges = sorted(
        [(one.attempt.sent_ms, 1) for one in evidence.requests]
        + [(one.attempt.ended_ms, -1) for one in evidence.requests]
    )
    peak = held = 0
    for _at, change in edges:
        held += change
        peak = max(peak, held)
    return peak


@dataclass(frozen=True)
class Executed:
    """One attempt as it was recorded: the contract's record, and what the record does not hold."""

    record: TrialRecord
    loads: tuple[Role, ...]
    peak_fan_out: int
    side_calls: int
    delegation: Delegation | None

    @property
    def duration_ms(self) -> int:
        """The task's wall time, tool waits included: from the attempt's start to its end."""
        return self.record.ended_ms - self.record.started_ms

    @property
    def output_tokens(self) -> int | None:
        usage = self.record.usage
        return None if usage is None else usage.output_tokens


@dataclass(frozen=True)
class CellRan:
    """A live cell: its background (`replay.CellRun`, with nothing sent at low load) and its attempts."""

    cell: PlannedCell
    background: replay.CellRun
    trials: tuple[Executed, ...]

    @property
    def background_students(self) -> frozenset[int]:
        """The students the background sent as."""
        return frozenset(one.student for one in self.background.requests)

    @property
    def complete(self) -> bool:
        """Whether every planned trial of the cell has an attempt that counts, and the cell was not
        stopped: an invalid attempt leaves it incomplete until it is replaced."""
        counted = {one.record.trial for one in self.trials if one.record.counts_as_attempt}
        stopped = self.background.record.status is CellStatus.INCOMPLETE
        return not stopped and counted == set(self.cell.trials)


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


def live_cells(plan: Plan) -> tuple[int, ...]:
    """The plan's live cells, in its order."""
    return tuple(cell.identity.cell for cell in plan.cells if cell.identity.phase in LIVE_PHASES)


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
        finished, started = asyncio.Event(), asyncio.Event()
        stops = replay.Stops(finished=finished, cancel=self.cancel)
        background = asyncio.ensure_future(
            replay.run_cell(
                self.plan,
                cell,
                self.gateway,
                self.keys,
                stops=stops,
                first_request=self.request,
                evidence=self.evidence,
                started=started,
            )
        )
        executed: list[Executed] = []
        try:
            waiting = asyncio.ensure_future(started.wait())
            try:
                await asyncio.wait({waiting, background}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiting.cancel()
            if not started.is_set():
                # The cell was refused before it started: its error is raised.
                background.result()
            for trial in planned.trials:
                if self.cancel.is_set():
                    break
                executed.append(await self.attempt(self.plan.workload.trials[trial]))
            # Only now does the background learn that the live work has ended.
            finished.set()
            ran = await background
        finally:
            background.cancel()
            await asyncio.gather(background, return_exceptions=True)
        self.request += len(ran.requests)
        return CellRan(planned, ran, tuple(executed))

    async def attempt(self, trial: TrialIdentity) -> Executed:
        async with self.harnesses.attempt(trial, self.keys[LIVE_STUDENT], self.cancel) as work:
            evidence = work.evidence
            if self.cancel.is_set() and evidence.invalid is None:
                evidence = replace(evidence, invalid=InvalidReason.INTERRUPTED)
            grade = None
            if evidence.may_pass:
                try:
                    grade = self.graders[_family(trial)](trial, work)
                except Exception:  # noqa: BLE001 - a grader that could not grade makes the attempt invalid
                    evidence = replace(evidence, invalid=InvalidReason.GRADER_ERROR)
            record = evidence.trial_record(
                execution=self.execution, trial=trial.trial, student=LIVE_STUDENT, grade=grade
            )
        self.execution += 1
        return Executed(
            record=record,
            loads=self.plan.loads_of(trial),
            peak_fan_out=peak_fan_out(evidence),
            side_calls=evidence.side_calls,
            delegation=work.delegation,
        )


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


def course_graders() -> dict[Family, Grader]:
    """The course's graders that read what an attempt left in its output folder: optimization,
    parallel analysis (with the delegation its harness reported) and grounded research. A grade is
    asked for only of an attempt within its limits. The debugging family's grader reads what its
    submitted program writes when it is run again in a fresh worker, which its caller does."""

    def graded(properties: tuple[bool, ...]) -> Grade:
        return Grade(properties=properties, within_limits=True)

    return {
        Family.OPTIMIZATION: lambda trial, work: graded(
            optimization.grade(trial.instance_id, work.artifacts).properties
        ),
        Family.ANALYSIS: lambda trial, work: graded(
            analysis.grade(trial.instance_id, work.artifacts, work.delegation).properties
        ),
        Family.RESEARCH: lambda trial, work: graded(research.grade(trial.instance_id, work.artifacts).properties),
    }


# --- The harnesses of a real run -------------------------------------------------------------


class TrialWorker(Protocol):
    """What the runner needs of a worker (`workers.Worker`)."""

    transcript: Path

    def run(self, command: Sequence[str]) -> workers.Ran: ...

    def collect(self) -> Path: ...

    def stop(self) -> None: ...

    def discard(self) -> list[str]: ...


@dataclass(frozen=True)
class Task:
    """What a trial is given: the folder of its task's files, its prompts in order (the first, then
    each follow-up), and its local tools by name."""

    inputs: Path
    prompts: tuple[str, ...]
    tools: Mapping[str, Path] = field(default_factory=dict)


class WorkerHarnesses:
    """The harnesses of a real run. Each attempt has a fresh worker (`start`, such as
    `workers.Workers.start`) of `image` under `limits`, and a forwarder of its own to the gateway
    at `upstream`, on the pool `pool`, holding the student's key and the plan's limits on `clock`,
    the run's. `tasks` gives what a trial is given."""

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
        task = next(
            task
            for task in self.plan.spec.suite.tasks
            if any(instance.instance_id == trial.instance_id for instance in task.instances)
        )
        return next(one for one in task.harnesses if one.harness is trial.harness)

    @contextlib.asynccontextmanager
    async def attempt(self, trial: TrialIdentity, key: str, cancel: asyncio.Event) -> AsyncGenerator[Work]:
        assignment, task = self.assignment(trial), self.tasks(trial)
        budget = Budget(self.plan.workload.limits, self.clock)
        forwarder = Forwarder(self.upstream, pool=self.pool, key=key, harness=trial.harness, budget=budget)
        held: list[TrialWorker] = []
        with forwarder, tempfile.TemporaryDirectory() as nothing:
            ran = asyncio.ensure_future(asyncio.to_thread(self._run, assignment, task, forwarder, held, Path(nothing)))
            stopped = asyncio.ensure_future(cancel.wait())
            try:
                await asyncio.wait({ran, stopped}, return_when=asyncio.FIRST_COMPLETED)
                if not ran.done():
                    await self._stop(forwarder, held)
                yield await asyncio.shield(ran)
            finally:
                stopped.cancel()
                if not ran.done():
                    await self._stop(forwarder, held)
                    await asyncio.wait({ran})
                left = [thing for worker in held for thing in await asyncio.to_thread(worker.discard)]
                if left:
                    error = workers.Undisposed("an attempt left something behind")
                    for thing in left:
                        error.add_note(f"not removed: {thing}")
                    raise error

    @staticmethod
    async def _stop(forwarder: Forwarder, held: Sequence[TrialWorker]) -> None:
        """Stops an attempt under way: its way out is closed and its worker ended."""
        forwarder.abort()
        for worker in held:
            with contextlib.suppress(workers.WorkerFault):
                await asyncio.to_thread(worker.stop)

    def _run(
        self,
        assignment: HarnessAssignment,
        task: Task,
        forwarder: Forwarder,
        held: list[TrialWorker],
        nothing: Path,
    ) -> Work:
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
        held.append(worker)
        adapter = Adapter(assignment, model=self.pool, tools=tuple(task.tools))
        trial = adapter.trial(worker, task.prompts, turns=lambda: forwarder.budget.model_turns)
        # The trial is over: nothing more is admitted, and what is in flight is settled.
        forwarder.abort()
        forwarder.drained(CANCEL_S)
        evidence = trial.ending.evidence(forwarder)
        artifacts = nothing
        try:
            if evidence.may_pass:
                artifacts = worker.collect()
            worker.stop()
        except workers.OverCap:
            # What it left does not fit the cap: none of it is kept, and the grader finds nothing.
            artifacts = nothing
        except (workers.WorkerFault, OSError):
            evidence = replace(evidence, invalid=evidence.invalid or InvalidReason.DRIVER_ERROR)
        return Work(evidence, artifacts, trial.delegation)
