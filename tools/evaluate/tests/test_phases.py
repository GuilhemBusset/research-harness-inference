"""The phase runner (`evaluate.phases`) on a reduced plan: a class of two, one diagnostic slot, two
tasks (one of each background shape) at both loads, and short windows.

Everything is made up and local: the gateway is an in-process transport that answers on a clock the
test moves, the workspace makes up its scrapes from that clock and from what the gateway received,
the harness sends its turns through the same gateway, and the journal is under `tmp_path`.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import common
from evaluate import booking, capacity, phases, plan, quality, replay, scripted, telemetry, trials, workers
from evaluate.contracts import (
    LIVE_PHASES,
    MAX_COUNT,
    CellStatus,
    Family,
    Grade,
    HostMetric,
    InstanceType,
    InvalidFlag,
    InvalidReason,
    Manifest,
    PathCategory,
    Phase,
    Profile,
    RequestRecord,
    Route,
    Run,
    Settings,
    Shape,
    Suite,
    Traffic,
    TrialOutcome,
    loads,
)
from evaluate.course import research, revision
from evaluate.forwarding import Evidence, Measurement
from evaluate.journal import AttemptStatus, Journal
from evaluate.streams import Attempts
from rhi_common.engine_metrics import VLLM
from rhi_common.keys import generate_key
from rhi_common.records import Session

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Sequence
    from pathlib import Path

COURSE = plan.load_suite()
POOL = "coursework"
ORIGIN_S = 1_000_000.0
START_MS = 600_000
ANSWER_MS = 1_000
LIVE_TURNS = 3
LIVE_WORK_MS = 30_000
SCRAPE_S = 15
YIELDS = 200
IMAGE, REVISION, FINGERPRINT = "sha256:" + "a1" * 32, "c3" * 20, "f6" * 32


def reduced() -> plan.Plan:
    """Two students; the sweep at one slot and at the class; one hands-on and one several-agents
    task, each at low load and at class size; short windows and short conversations."""
    tasks = tuple(task for task in COURSE.suite.tasks if task.task_id in ("debugging.t01", "research.t01"))
    profile = plan.ProfileSpec(
        repetitions=1,
        attempts=1,
        loads=plan.LOADS,
        core_only=True,
        anchor_only=True,
        instances_per_task=1,
        table={"reduced": True},
    )
    short = {
        "opening_tokens": 8,
        "transcript_tokens": [16, 32],
        "turn_input_tokens": [2, 6],
        "turn_output_tokens": 4,
        "tool_delay_ms": [500, 1000],
    }
    spec = replace(
        COURSE,
        suite=Suite(version=COURSE.suite.version, tasks=tasks),
        protocol=COURSE.protocol | {"replay": COURSE.protocol["replay"] | short},
        sweep_slots=(1,),
        diagnostic_admissions_ms=90_000,
        class_admissions_ms=90_000,
        opening_window_ms=20_000,
        limits=replace(COURSE.limits, drain_ms=5_000, trial_wall_time_ms=60_000),
        profiles=COURSE.profiles | {Profile.SCREEN: profile},
    )
    return plan.expand(spec, Profile.SCREEN, 2)


PLAN = reduced()


class Clock:
    """A clock the test moves: each time every task waits, the time goes to the earliest wait's end."""

    def __init__(self) -> None:
        self.at = START_MS
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


@dataclass
class Late:
    """The clock as a driver with too much to do reads it, from `from_ms` on: every wait ends late."""

    clock: Clock
    late_ms: int
    from_ms: int = 0

    def now_ms(self) -> int:
        return self.clock.now_ms()

    async def sleep_ms(self, ms: int) -> None:
        late = self.late_ms if self.clock.now_ms() >= self.from_ms else 0
        await self.clock.sleep_ms(ms + late)


def _event(data: object, name: str | None = None) -> bytes:
    head = f"event: {name}\n" if name else ""
    return f"{head}data: {data if isinstance(data, str) else json.dumps(data)}\n\n".encode()


def _answer(route: Route, cached: int) -> list[bytes]:
    """A streamed answer that calls the route warm-up's tool, with a cached input of `cached`."""
    tool = phases.WARM_UP_TOOL
    if route is Route.MESSAGES:
        usage = {"input_tokens": 30, "cache_read_input_tokens": cached}
        block = {"type": "tool_use", "id": "call_1", "name": tool, "input": {}}
        return [
            _event({"type": "message_start", "message": {"usage": usage}}, "message_start"),
            _event({"type": "content_block_start", "index": 0, "content_block": block}, "content_block_start"),
            _event(
                {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{}"}},
                "content_block_delta",
            ),
            _event({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
            _event({"type": "message_stop"}, "message_stop"),
        ]
    if route is Route.RESPONSES:
        call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": tool, "arguments": "{}"}
        usage = {"input_tokens": 30, "input_tokens_details": {"cached_tokens": cached}, "output_tokens": 4}
        done = {"type": "response.completed", "response": {"usage": usage}}
        return [
            _event({"type": "response.output_item.added", "item": call}, "response.output_item.added"),
            _event({"type": "response.function_call_arguments.delta", "delta": "{}"}),
            _event(done, "response.completed"),
        ]
    delta = {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": tool, "arguments": "{}"}}]}
    return [
        _event({"choices": [{"index": 0, "delta": delta}]}),
        _event({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
        _event({"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 4}}),
        _event("[DONE]"),
    ]


_ROUTES = {
    "/v1/messages": Route.MESSAGES,
    "/v1/responses": Route.RESPONSES,
    "/v1/chat/completions": Route.CHAT_COMPLETIONS,
}


class Gateway(httpx.AsyncBaseTransport):
    """A made-up gateway: it knows a student by the key, answers after `ANSWER_MS`, reports a
    cached input of zero for a student's first request under a prefix and 12 after, and keeps when
    each request arrived and from whom. `answer`, when set, writes the answers instead."""

    def __init__(self, clock: Clock, keys: Sequence[str]) -> None:
        self.clock, self.keys = clock, list(keys)
        self.arrived: list[tuple[int, int]] = []
        self.open = 0
        self.on_arrival: Callable[[int, int], None] = lambda _at, _student: None
        self.answer: Callable[[Route, int], list[bytes]] | None = None
        self._cached: set[tuple[int, str]] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        student = self.keys.index(request.headers["authorization"].removeprefix("Bearer "))
        self.arrived.append((self.clock.now_ms(), student))
        self.on_arrival(self.clock.now_ms(), student)
        body = json.loads(request.content)
        prefix = json.dumps([body.get("system"), body.get("instructions"), body.get("tools")])
        cached = 12 if (student, prefix) in self._cached else 0
        self._cached.add((student, prefix))
        events = (self.answer or _answer)(_ROUTES[request.url.path], cached)

        async def stream() -> AsyncIterator[bytes]:
            self.open += 1
            try:
                yield events[0]
                await self.clock.sleep_ms(ANSWER_MS)
                for one in events[1:]:
                    yield one
            finally:
                self.open -= 1

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())


_SPAN = re.compile(r"\[(\d+)(ms|s)\]$")


@dataclass
class Workspace:
    """A Prometheus workspace that answers a range selector with what it scraped every 15 seconds
    up to now: two hosts, and the gateway's count of each canary's requests. Each student's gateway
    affinity is a host (even students the first, odd ones the second), whose engine counts 20
    output tokens for each request of theirs; its input counters rise with the time. `change_at`
    replaces the second host by a third from that time on, `lifetime` is what each engine counted
    before, and `misattributed` makes the gateway count every request as the first canary's."""

    clock: Clock
    gateway: Gateway
    aliases: Sequence[str]
    change_at: float | None = None
    lifetime: float = 0.0
    misattributed: bool = False
    series: dict[str, list[tuple[dict[str, str], Callable[[float], float], Callable[[float], bool]]]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        held = telemetry.selectors(POOL, VLLM)
        always = lambda _t: True
        for affinity, host, present in (
            (0, "node-a", always),
            (1, "node-b", lambda t: self.change_at is None or t < self.change_at),
            (1, "node-c", lambda t: self.change_at is not None and t >= self.change_at),
        ):
            for key, value, labels in (
                (
                    telemetry.Source.RELEASE,
                    1,
                    {"instance_type": "g7e.2xlarge", "image_digest": IMAGE, "model_revision": REVISION},
                ),
                (telemetry.Source.LAUNCH, 1, {"settings_fingerprint": FINGERPRINT}),
                (telemetry.Source.CACHE_CAPACITY, 400_000, {}),
                (telemetry.Source.CACHE_USAGE, 0.25, {"engine": "0"}),
                (HostMetric.RUNNING_REQUESTS, 4, {"engine": "0"}),
                (HostMetric.WAITING_REQUESTS, 0, {"engine": "0"}),
                (HostMetric.PREEMPTIONS_TOTAL, 0, {"engine": "0"}),
            ):
                self.add(held[key], {"instance": host, **labels}, lambda _t, v=value: v, present)
            engine = {"instance": host, "engine": "0"}
            self.add(held[HostMetric.OUTPUT_TOKENS_TOTAL], engine, self.output(affinity), present)
            for key, rate in ((HostMetric.INPUT_TOKENS_TOTAL, 100), (HostMetric.CACHED_INPUT_TOKENS_TOTAL, 50)):
                self.add(held[key], engine, lambda t, r=rate: self.lifetime + 50 + r * (t - ORIGIN_S + 900), present)
        for student, alias in enumerate(self.aliases):
            self.add(
                held[telemetry.Source.GATEWAY_REQUESTS],
                {"instance": "gateway-task", "alias": alias},
                self.count(student),
                always,
            )

    def add(
        self, selector: str, labels: dict[str, str], value: Callable[[float], float], present: Callable[[float], bool]
    ) -> None:
        self.series.setdefault(selector, []).append(({"pool": POOL, **labels}, value, present))

    def _before(self, at: float, students: Callable[[int], bool]) -> int:
        return sum(1 for arrived, one in self.gateway.arrived if students(one) and ORIGIN_S + arrived / 1000 < at)

    def output(self, affinity: int) -> Callable[[float], float]:
        return lambda at: self.lifetime + 50 + 20 * self._before(at, lambda one: one % 2 == affinity)

    def count(self, student: int) -> Callable[[float], float]:
        def counted(at: float) -> float:
            if self.misattributed:
                return self._before(at, lambda _one: True) if student == 0 else 0
            return self._before(at, lambda one: one == student)

        return counted

    def read_samples(self, query: str, *, at: float) -> list[common.Series]:
        span = _SPAN.search(query)
        assert span is not None, query
        seconds = int(span[1]) / (1000 if span[2] == "ms" else 1)
        now = ORIGIN_S + self.clock.now_ms() / 1000
        first = int((at - seconds - ORIGIN_S) // SCRAPE_S)
        scrapes = [ORIGIN_S + 3 + SCRAPE_S * k for k in range(first, first + int(seconds // SCRAPE_S) + 3)]
        found = []
        for labels, value, present in self.series.get(query[: span.start()], []):
            samples = tuple((t, float(value(t))) for t in scrapes if at - seconds < t <= min(at, now) and present(t))
            if samples:
                found.append(common.Series(labels, samples))
        return found

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        raise AssertionError("the reader takes samples as they were scraped")


def run_here(scratch: Path) -> trials.Execute:
    """`trials.Execute` on this host: the command run with this Python in a copy of the files."""
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


def reference_work(trial: Any, folder: Path) -> tuple[Path, trials.Execute | None]:
    """What an attempt that solved its instance leaves for the course's grader."""
    if trials.task_of(PLAN, trial).family is Family.RESEARCH:
        return research.reference_artifacts(trial.instance_id), None
    folder.mkdir(parents=True)
    shutil.copyfile(revision.reference_solver(trial.instance_id), folder / revision.SOLVER)
    return folder, run_here(folder.parent / f"{folder.name}-runs")


@dataclass
class Harness:
    """A harness that sends its turns through the gateway as the live seat, then works a while
    unless it is told to stop, and leaves its instance's reference answer. `silent` trials send
    nothing; `on_turn` sees each turn sent."""

    clock: Clock
    gateway: Gateway
    artifacts: Path
    silent: frozenset[int] = frozenset()
    on_turn: Callable[[int, int], None] = lambda _trial, _turn: None
    # How long before its dispatch each later turn reached the forwarder, and a tally that lost its completions.
    lag_ms: int = 0
    miscounted: bool = False
    attempts: itertools.count[int] = field(default_factory=itertools.count)

    @contextlib.asynccontextmanager
    async def attempt(self, trial: Any, key: str, cancel: asyncio.Event) -> AsyncGenerator[trials.Work]:
        clock, started = self.clock, self.clock.now_ms()
        attempts, measured = Attempts(), []
        async with httpx.AsyncClient(base_url="https://gateway.example.com", transport=self.gateway) as http:
            for turn in range(0 if trial.trial in self.silent else LIVE_TURNS):
                if cancel.is_set():
                    break
                sent = clock.now_ms()
                reader = attempts.send(Route.MESSAGES, sent)
                body = {
                    "system": f"live trial {trial.trial}",
                    "tools": [],
                    "messages": [{"role": "user", "content": "go"}],
                }
                headers = {"authorization": f"Bearer {key}"}
                async with http.stream("POST", "/v1/messages", headers=headers, content=json.dumps(body)) as answer:
                    reader.answered(answer.status_code)
                    async for chunk in answer.aiter_bytes():
                        reader.feed(chunk, clock.now_ms())
                one = reader.end(clock.now_ms())
                measured.append(
                    Measurement(
                        attempt=one,
                        received_ms=sent - (self.lag_ms if turn else 0),
                        side_call=False,
                        closed_by_client=False,
                        cancelled=False,
                        allowance_tokens=16,
                        charged_tokens=4,
                    )
                )
                self.on_turn(trial.trial, turn)
            waits = [asyncio.ensure_future(cancel.wait()), asyncio.ensure_future(clock.sleep_ms(LIVE_WORK_MS))]
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for one in waits:
                one.cancel()
        evidence = Evidence(
            started_ms=started,
            ended_ms=clock.now_ms(),
            failure=None,
            invalid=None,
            model_turns=len(measured),
            side_calls=0,
            retries=0,
            refusals=0,
            generated_tokens=4 * len(measured),
            open_reservations=0,
            tally=replace(attempts.tally(), completed=0) if self.miscounted else attempts.tally(),
            requests=tuple(measured),
        )
        artifacts, execute = reference_work(trial, self.artifacts / f"attempt-{next(self.attempts)}")
        yield trials.Work(evidence=evidence, artifacts=artifacts, execute=execute)


def grade_all(trial: Any, _work: trials.Work) -> Grade:
    """A grader other than the course's: it passes every attempt."""
    return Grade(properties=(True,) * trials.task_of(PLAN, trial).required_properties, within_limits=True)


VERSIONS = {"claude": "2.1.323 (Claude Code)", "codex": "codex-cli 0.169.0", "opencode": "1.19.5", "pi": "1.5.2"}
DISTRIBUTIONS = {"openai": "3.57.0", "anthropic": "2.11.0", "highspy": "1.16.0"}


def trial_image(versions: dict[str, str]) -> Callable[[Sequence[str]], str]:
    def run(command: Sequence[str]) -> str:
        if command[-1] == "--version":
            return "v24.22.3" if command[0] == "node" else versions[command[0]]
        return DISTRIBUTIONS[command[-1]]

    return run


RUN = Run(
    ordinal=0,
    source_commit="ab" * 20,
    lock_digest="sha256:" + "11" * 32,
    harness_image_digest="sha256:" + "22" * 32,
    driver_image_digest="sha256:" + "33" * 32,
    driver_cpus=4,
    driver_memory_mib=8192,
    path_category=PathCategory.IN_REGION,
)
_ASKED: dict[str, Any] = dict.fromkeys(Settings.__dataclass_fields__)
_ASKED |= {"host_count": 2, "instance_type": InstanceType.G7E_2XLARGE}
REQUESTED = Settings(**_ASKED)


@dataclass
class Bench:
    """One run of a plan in a made-up booking, and everything it was run against."""

    tmp_path: Path
    planned: plan.Plan = PLAN
    keys: int | None = None
    aliases: Sequence[str] | None = None
    late: tuple[int, int] | None = None
    versions: dict[str, str] = field(default_factory=lambda: dict(VERSIONS))
    breach_at_ms: int | None = None
    deadline_ms: int | None = None
    clock: Clock = field(default_factory=Clock)
    pool: str = POOL
    instances: int = 2
    requested: Settings | None = None
    graders: dict[Any, Any] | None = None
    # The journal of an earlier run to go on with; a new one by default.
    resumes: Journal | None = None
    continuity: Callable[[], Coroutine[Any, Any, booking.Reason | None]] | None = None

    def __post_init__(self) -> None:
        count = self.planned.canary_identities if self.keys is None else self.keys
        self.key_list = [generate_key() for _ in range(count)]
        self.gateway = Gateway(self.clock, self.key_list)
        alias_list = [f"canary-{at:02d}" for at in range(count)] if self.aliases is None else list(self.aliases)
        self.alias_list = alias_list
        self.workspace = Workspace(self.clock, self.gateway, alias_list)
        self.harness = Harness(self.clock, self.gateway, self.tmp_path)
        self.cancel = asyncio.Event()
        self.journal: Journal = Journal.create(self.tmp_path / "runs") if self.resumes is None else self.resumes
        self.manifest_before_first_request: bool | None = None
        previous = self.gateway.on_arrival

        def first_arrival(at: int, student: int) -> None:
            if self.manifest_before_first_request is None:
                self.manifest_before_first_request = (self.journal.directory / phases.MANIFEST).is_file()
            previous(at, student)

        self.gateway.on_arrival = first_arrival

    def run(self) -> phases.Result:
        clock = self.clock if self.late is None else Late(self.clock, self.late[0], self.late[1])
        gateway = replay.Gateway("https://gateway.example.com", POOL, clock, self.gateway)
        session = Session(
            pool=self.pool,
            instances=self.instances,
            starts_at=int(ORIGIN_S),
            ends_at=int(ORIGIN_S) + 6 * 3600,
            set_by="evaluate/run",
            set_at=int(ORIGIN_S),
        )
        outcome = booking.Outcome(reason=None, session=session, cleanup=booking.Cleanup.HELD)

        async def continuity() -> booking.Reason | None:
            breached = self.breach_at_ms is not None and self.clock.now_ms() >= self.breach_at_ms
            return booking.Reason.BOOKING_CHANGED if breached else None

        source = phases.Telemetry(
            self.workspace, POOL, VLLM, telemetry.Requested(self.requested or REQUESTED, FINGERPRINT)
        )
        return self.clock.drive(
            phases.run(
                self.planned,
                phases.Admitted(outcome, continuity if self.continuity is None else self.continuity),
                gateway,
                phases.Canaries(tuple(self.key_list), tuple(self.alias_list)),
                source,
                phases.Artifacts(RUN, trial_image(self.versions)),
                self.harness,
                trials.course_graders() if self.graders is None else self.graders,
                self.journal,
                origin_s=ORIGIN_S,
                cancel=self.cancel,
                deadline_ms=self.deadline_ms,
            )
        )

    def refused(self) -> phases.Refusal:
        with pytest.raises(phases.PhaseError) as refused:
            self.run()
        # Nothing reached the gateway, and no cell was opened.
        assert self.gateway.arrived == []
        assert self.journal.attempts == ()
        return refused.value.reason


def capacity_families() -> set[Any]:
    return {trials.task_of(PLAN, trial).family for trial in PLAN.workload.trials}


def running(bench: Bench) -> int | None:
    """The cell whose attempt is the journal's last, `None` before the first."""
    attempts = bench.journal.attempts
    return attempts[-1].cell if attempts else None


def phase_of(result: phases.CellResult) -> Phase:
    return result.cell.identity.phase


def graded(result: phases.Result) -> capacity.Capacity:
    return capacity.read(PLAN, result.evidence, usd_per_host_hour=1.0)


def cannot_qualify(bench: Bench, result: phases.Result, cell: phases.CellResult) -> None:
    """The cell cannot contribute a qualifying result: not valid, read invalid offline, and run
    again by a resume."""
    assert not cell.qualifies
    readings = {one.identity.cell: one for one in graded(result).cells}
    reading = readings.get(cell.cell.identity.cell)
    assert reading is None or reading.verdict is capacity.Verdict.INVALID
    reserved = [(one.identity.cell, 0) for one in PLAN.cells]
    resumed = bench.journal.resume(reserved, 0, PLAN.workload_hash, result.manifest.treatment_id)
    assert all(one.cell != cell.cell.identity.cell for one in resumed.reused)


# --- A whole reduced plan ------------------------------------------------------------------------


def test_a_reduced_plan_runs_every_phase_and_keeps_warm_up_and_openings_out_of_steady_state(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    result = bench.run()

    assert result.ended is phases.Ended.COMPLETED
    assert [one.cell.identity.cell for one in result.cells] == [one.identity.cell for one in PLAN.cells]
    assert {phase_of(one) for one in result.cells} == set(Phase)
    # The route warm-up: one streamed exchange on each wire format, as warm-up traffic.
    warm_up = result.cells[0]
    routes = [one.route for one in warm_up.requests if one.conversation < 3]
    assert routes == [Route.MESSAGES, Route.RESPONSES, Route.CHAT_COMPLETIONS]
    # The per-host warm-up sent as every canary: each one's affinity is a host, and both were warmed.
    assert {one.student for one in warm_up.requests} == set(range(len(bench.key_list)))
    assert all(host.warmed for one in result.cells[1:] if one.reading for host in one.reading.hosts)
    # Every cell was read over its whole window, reconciled and checkpointed with its observations.
    for one in result.cells:
        assert one.qualifies, (one.cell.identity, one.record)
        assert one.record is not None and one.record.hosts_observed == 2 and one.reading is not None
        assert {sample.host for sample in one.samples} == {host.host for host in one.reading.hosts}
        saved = bench.journal.observations(one.attempt)
        assert len(saved) == len(one.requests) + len(one.trials) + len(one.samples)

    # Capacity records for both shapes, graded offline from what the runner emitted.
    reading = graded(result)
    assert set(reading.shapes) == set(Shape)
    floors = [cell for cell in reading.cells if cell.class_floor]
    assert {cell.identity.shape for cell in floors} == set(Shape)
    assert all(cell.verdict is not capacity.Verdict.INVALID for cell in reading.cells), [
        (cell.identity, cell.reasons) for cell in reading.cells
    ]
    # Live-task records for both shapes, at both loads.
    trial_records = result.trials
    assert trial_records and all(record.outcome is TrialOutcome.PASSED for record in trial_records)
    held = {
        (
            PLAN.cells[PLAN.workload.trials[r.trial].cell].identity.shape,
            PLAN.cells[PLAN.workload.trials[r.trial].cell].identity.phase,
        )
        for r in trial_records
    }
    assert held == {(shape, phase) for shape in Shape for phase in LIVE_PHASES}
    live = [one for one in result.cells if phase_of(one) in LIVE_PHASES]
    assert all(any(request.traffic is Traffic.LIVE for request in one.requests) for one in live)

    # The warm-up and the openings sent, and none of it is a steady-state sample.
    outside = {one.cell.identity.cell for one in result.cells if phase_of(one) in (Phase.WARM_UP, Phase.CLASS_OPENING)}
    assert all(any(one.requests) for one in result.cells if one.cell.identity.cell in outside)
    steady = result.steady_state()
    assert steady and not any(one.cell in outside or one.traffic is Traffic.WARM_UP for one in steady)
    assert all(not cell.steady_state for cell in reading.cells if cell.identity.cell in outside)

    # The manifest was written whole before anything was sent, and observed what served the run.
    assert bench.manifest_before_first_request is True
    manifest = loads((bench.journal.directory / phases.MANIFEST).read_text(), Manifest)
    assert manifest.workload_id == PLAN.workload_hash and manifest.run == RUN
    assert manifest.observed.host_count == 2 and manifest.observed.engine_image_digest == IMAGE


# --- What runs is what the plan pins --------------------------------------------------------------


def _other_grader(planned: plan.Plan) -> plan.Plan:
    tasks = tuple(
        replace(task, instances=tuple(replace(one, grader_digest="sha256:" + "0" * 64) for one in task.instances))
        for task in planned.spec.suite.tasks
    )
    spec = replace(planned.spec, suite=replace(planned.spec.suite, tasks=tasks))
    return replace(planned, spec=spec)


def _sent_otherwise(planned: plan.Plan) -> plan.Plan:
    """The plan with the replay's sizes changed after it was expanded: its workload names others."""
    protocol = planned.spec.protocol
    replay_table = protocol["replay"] | {"turn_output_tokens": 7}
    return replace(planned, spec=replace(planned.spec, protocol=protocol | {"replay": replay_table}))


@pytest.mark.parametrize(
    ("planned", "versions"),
    [
        (PLAN, VERSIONS | {"claude": "2.1.999 (Claude Code)"}),
        (
            replace(
                PLAN,
                spec=replace(PLAN.spec, protocol=PLAN.spec.protocol | {"replay_fixture_digest": "sha256:" + "0" * 64}),
            ),
            VERSIONS,
        ),
        (_other_grader(PLAN), VERSIONS),
        (_sent_otherwise(PLAN), VERSIONS),
    ],
    ids=["harness-version", "replay-fixture", "grader", "protocol"],
)
def test_artifacts_that_are_not_the_plans_are_refused_before_measuring(
    tmp_path: Path, planned: plan.Plan, versions: dict[str, str]
) -> None:
    bench = Bench(tmp_path, planned=planned, versions=versions)
    assert bench.refused() is phases.Refusal.ARTIFACT_MISMATCH
    assert not (bench.journal.directory / phases.MANIFEST).exists()


def test_a_family_with_no_grader_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    assert Bench(tmp_path, graders={}).refused() is phases.Refusal.ARTIFACT_MISMATCH


def test_a_grader_other_than_the_one_the_manifest_identifies_is_refused(tmp_path: Path) -> None:
    substitute = trials.course_graders() | {Family.RESEARCH: grade_all}
    assert Bench(tmp_path, graders=substitute).refused() is phases.Refusal.ARTIFACT_MISMATCH


def test_a_journal_of_another_workload_keeps_its_manifest_and_is_not_resumed(tmp_path: Path) -> None:
    first = Bench(tmp_path / "first")
    first.run()
    saved = (first.journal.directory / phases.MANIFEST).read_text()
    other = plan.expand(PLAN.spec, Profile.SCREEN, 2, input_seed=PLAN.workload.input_seed + 1)
    again = Bench(tmp_path / "again", planned=other, resumes=first.journal)
    with pytest.raises(phases.PhaseError) as refused:
        again.run()
    assert refused.value.reason is phases.Refusal.INCOMPATIBLE_RESUME
    assert again.gateway.arrived == []
    assert (first.journal.directory / phases.MANIFEST).read_text() == saved


def test_the_manifest_resolves_what_is_installed() -> None:
    manifest = phases.resolve_manifest(PLAN, phases.Artifacts(RUN, trial_image(VERSIONS)), REQUESTED)
    assert manifest.workload == PLAN.workload and manifest.requested == REQUESTED
    assert (
        phases.installed_versions(trial_image(VERSIONS))[PLAN.workload.harnesses[0].harness]
        == PLAN.workload.harnesses[0].version
    )
    assert PLAN.spec.protocol["replay_fixture_digest"] == plan.file_digest(plan.REPLAY_FIXTURE)


# --- Faults that leave a cell unable to qualify ---------------------------------------------------


def test_a_host_change_leaves_its_cell_and_the_ones_after_unqualified(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    floor = next(
        one for one in PLAN.cells if plan.Role.CLASS_FLOOR in one.roles and one.identity.shape is Shape.HANDS_ON
    )
    seen = bench.gateway.on_arrival

    def change(at: int, student: int) -> None:
        # The second student first sends in the hands-on class-size cell: a host is replaced 5 s later.
        if student == 1 and running(bench) == floor.identity.cell and bench.workspace.change_at is None:
            bench.workspace.change_at = ORIGIN_S + at / 1000 + 5
        seen(at, student)

    bench.gateway.on_arrival = change
    result = bench.run()
    by_cell = {one.cell.identity.cell: one for one in result.cells}
    changed = by_cell[floor.identity.cell]
    assert changed.record is not None and InvalidFlag.HOST_TURNOVER in changed.record.invalid_flags
    assert all(by_cell[cell].qualifies for cell in range(floor.identity.cell))
    for one in result.cells[floor.identity.cell :]:
        cannot_qualify(bench, result, one)


@pytest.mark.parametrize(("pool", "instances"), [("other-pool", 2), (POOL, 1)], ids=["pool", "hosts"])
def test_traffic_and_telemetry_of_another_pool_than_booked_are_refused(
    tmp_path: Path, pool: str, instances: int
) -> None:
    # The gateway and the workspace serve the pool `POOL` on two hosts.
    assert Bench(tmp_path, pool=pool, instances=instances).refused() is phases.Refusal.BOOKING_MISMATCH


def test_identities_too_few_or_shared_are_refused_before_anything_is_sent(tmp_path: Path) -> None:
    needed = PLAN.canary_identities
    assert Bench(tmp_path / "few", keys=needed - 1).refused() is phases.Refusal.INSUFFICIENT_IDENTITIES
    shared = ["canary-00"] * needed
    assert Bench(tmp_path / "shared", aliases=shared).refused() is phases.Refusal.INSUFFICIENT_IDENTITIES


def test_a_live_seat_that_sent_nothing_leaves_its_cell_unqualified(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    low = next(one for one in PLAN.cells if one.identity.phase is Phase.LIVE_LOW_LOAD)
    bench.harness.silent = frozenset(low.trials)
    result = bench.run()
    silent = next(one for one in result.cells if one.cell == low)
    assert silent.record is not None and InvalidFlag.MISSING_EVIDENCE in silent.record.invalid_flags
    cannot_qualify(bench, result, silent)


def test_a_release_that_changes_inside_a_window_leaves_that_cell_unqualified(tmp_path: Path) -> None:
    bench = Bench(tmp_path, requested=replace(REQUESTED, engine_image_digest=IMAGE))
    sweep = next(one for one in PLAN.cells if one.identity.phase is Phase.CAPACITY_SWEEP)
    rows = bench.workspace.series[telemetry.selectors(POOL, VLLM)[telemetry.Source.RELEASE]]
    changed: list[float] = []

    def other(at: float) -> bool:
        return bool(changed) and changed[0] <= at < changed[0] + 20

    labels, value, present = rows[0]
    rows[0] = labels, value, lambda at: present(at) and not other(at)
    rows.append((labels | {"image_digest": "sha256:" + "bb" * 32}, value, other))

    def change(at: int, _student: int) -> None:
        if running(bench) == sweep.identity.cell and not changed:
            changed.append(ORIGIN_S + at / 1000 + 30)

    bench.gateway.on_arrival = change
    result = bench.run()
    cell = next(one for one in result.cells if one.cell == sweep)
    assert cell.record is not None and InvalidFlag.HOST_TURNOVER in cell.record.invalid_flags
    cannot_qualify(bench, result, cell)


def test_a_launch_fingerprint_that_changes_inside_a_window_leaves_that_cell_unqualified(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    sweep = next(one for one in PLAN.cells if one.identity.phase is Phase.CAPACITY_SWEEP)
    rows = bench.workspace.series[telemetry.selectors(POOL, VLLM)[telemetry.Source.LAUNCH]]
    changed: list[float] = []

    def other(at: float) -> bool:
        return bool(changed) and changed[0] <= at < changed[0] + 30

    # One host reports another launch for 30 seconds, then the requested one again.
    labels, value, present = rows[0]
    rows[0] = labels, value, lambda at: present(at) and not other(at)
    rows.append((labels | {"settings_fingerprint": "ba" * 32}, value, other))

    def change(at: int, _student: int) -> None:
        if running(bench) == sweep.identity.cell and not changed:
            changed.append(ORIGIN_S + at / 1000 + 25)

    bench.gateway.on_arrival = change
    result = bench.run()
    cell = next(one for one in result.cells if one.cell == sweep)
    assert cell.record is not None and InvalidFlag.HOST_TURNOVER in cell.record.invalid_flags
    cannot_qualify(bench, result, cell)


def test_a_gateway_that_does_not_count_each_canary_leaves_its_cells_unqualified(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.workspace.misattributed = True
    result = bench.run()
    shared = [one for one in result.cells if one.cell.identities > 1]
    assert shared
    for one in shared:
        assert one.record is not None and InvalidFlag.UNRECONCILED_COUNTERS in one.record.invalid_flags
        cannot_qualify(bench, result, one)


def test_a_live_attempt_whose_first_answer_reads_a_cache_leaves_its_cell_unqualified(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    low = next(one for one in PLAN.cells if one.identity.phase is Phase.LIVE_LOW_LOAD)
    bench.gateway.answer = lambda route, cached: _answer(route, 12 if running(bench) == low.identity.cell else cached)
    result = bench.run()
    cell = next(one for one in result.cells if one.cell == low)
    assert cell.record is not None and InvalidFlag.MISSING_EVIDENCE in cell.record.invalid_flags
    cannot_qualify(bench, result, cell)


def test_a_later_serial_miss_does_not_certify_a_cached_first_live_answer(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    low = next(one for one in PLAN.cells if one.identity.phase is Phase.LIVE_LOW_LOAD)
    served: list[int] = []

    # The first answer of the cell read a cache; the next, sent as it ended, read none.
    def answer(route: Route, cached: int) -> list[bytes]:
        if running(bench) == low.identity.cell:
            cached = 0 if served else 12
            served.append(cached)
        return _answer(route, cached)

    bench.gateway.answer = answer
    result = bench.run()
    assert served[:2] == [12, 0]
    cell = next(one for one in result.cells if one.cell == low)
    assert cell.record is not None and InvalidFlag.MISSING_EVIDENCE in cell.record.invalid_flags
    cannot_qualify(bench, result, cell)


@pytest.mark.parametrize(
    ("harness", "flag"),
    [({"lag_ms": 500}, InvalidFlag.DRIVER_SATURATION), ({"miscounted": True}, InvalidFlag.UNRECONCILED_COUNTERS)],
    ids=["dispatch-lag", "tally"],
)
def test_live_requests_that_lag_or_do_not_reconcile_leave_their_cells_unqualified(
    tmp_path: Path, harness: dict[str, Any], flag: InvalidFlag
) -> None:
    bench = Bench(tmp_path)
    bench.harness = replace(bench.harness, **harness)
    result = bench.run()
    live = [one for one in result.cells if phase_of(one) in LIVE_PHASES]
    assert live
    for one in live:
        assert one.record is not None and flag in one.record.invalid_flags
        cannot_qualify(bench, result, one)


def test_a_route_that_answers_without_the_tool_call_ends_the_warm_up(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    text = scripted._responses(["plain text"], POOL)[1]
    bench.gateway.answer = lambda route, cached: text if route is Route.RESPONSES else _answer(route, cached)
    result = bench.run()
    assert result.ended is phases.Ended.ROUTE_WARM_UP_FAILED
    (warm_up,) = result.cells
    assert not warm_up.checkpointed and len(warm_up.requests) == 3


def test_offline_host_counters_are_what_the_window_counted(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    # Engines that counted beyond the contracts' bound before the run, and one whose preemptions
    # are two series: one restarts from five to zero while the other rises from zero to five.
    bench.workspace.lifetime = MAX_COUNT
    sweep = next(one for one in PLAN.cells if one.identity.phase is Phase.CAPACITY_SWEEP)
    rows = bench.workspace.series[telemetry.selectors(POOL, VLLM)[HostMetric.PREEMPTIONS_TOTAL]]
    changed: list[float] = []
    labels, _, present = rows[0]
    rows[0] = labels, lambda at: 0 if changed and at >= changed[0] else 5, present
    rows.append((labels | {"engine": "1"}, lambda at: 5 if changed and at >= changed[0] else 0, present))

    def change(at: int, _student: int) -> None:
        if running(bench) == sweep.identity.cell and not changed:
            changed.append(ORIGIN_S + at / 1000 + 30)

    bench.gateway.on_arrival = change
    result = bench.run()
    offline = {one.identity.cell: one for one in graded(result).cells}
    measured = [one for one in result.cells if phase_of(one) is Phase.CAPACITY_SWEEP]
    assert measured
    for one in measured:
        engine = offline[one.cell.identity.cell].engine
        assert engine is not None and one.reading is not None
        counted = one.reading.pool.counters
        assert engine.output_tokens == counted[HostMetric.OUTPUT_TOKENS_TOTAL] and engine.output_tokens
        assert engine.preemptions == counted[HostMetric.PREEMPTIONS_TOTAL]
    restarted = offline[sweep.identity.cell].engine
    assert restarted is not None and restarted.preemptions == 5


def test_missed_driver_headroom_refuses_the_run_before_the_gateway_sees_anything(tmp_path: Path) -> None:
    tolerance = PLAN.spec.protocol["driver"]["scheduling_lag_p95_ms_max"]
    assert Bench(tmp_path, late=(tolerance + 50, 0)).refused() is phases.Refusal.DRIVER_HEADROOM_MISSED


def test_a_driver_that_lags_in_a_cell_flags_it_saturated(tmp_path: Path) -> None:
    tolerance = PLAN.spec.protocol["driver"]["scheduling_lag_p95_ms_max"]
    # The driver keeps up with the check, then falls behind once the cells run.
    bench = Bench(tmp_path, late=(tolerance + 50, START_MS + phases.HEADROOM_MS + 1))
    result = bench.run()
    sweep = [one for one in result.cells if phase_of(one) is Phase.CAPACITY_SWEEP]
    assert sweep
    for one in sweep:
        assert one.record is not None and InvalidFlag.DRIVER_SATURATION in one.record.invalid_flags
        cannot_qualify(bench, result, one)


# --- Stopping -------------------------------------------------------------------------------------


def test_a_cancellation_during_a_live_seat_stops_everything_within_the_drain_and_keeps_what_it_saw(
    tmp_path: Path,
) -> None:
    bench = Bench(tmp_path)
    class_load = next(one for one in PLAN.cells if one.identity.phase is Phase.LIVE_CLASS_LOAD)
    cancelled_at: list[int] = []

    def cancel(trial: int, turn: int) -> None:
        if trial in class_load.trials and turn == 0 and not cancelled_at:
            cancelled_at.append(bench.clock.now_ms())
            bench.cancel.set()

    bench.harness.on_turn = cancel
    result = bench.run()

    assert result.ended is phases.Ended.CANCELLED
    last = result.cells[-1]
    assert last.cell == class_load and not last.checkpointed
    # Traffic stopped within the cell's drain and the worker's bound, and nothing is left open.
    assert bench.clock.now_ms() - cancelled_at[0] <= class_load.drain_ms + 5_000
    assert bench.gateway.open == 0
    assert not any(at > bench.clock.now_ms() for at, _ in bench.gateway.arrived)
    # The live attempt is interrupted, and its partial evidence stays readable in the journal.
    (record,) = last.trials
    assert record.outcome is TrialOutcome.INVALID and record.invalid is InvalidReason.INTERRUPTED
    attempt = bench.journal.attempts[last.attempt]
    assert attempt.status is AttemptStatus.INTERRUPTED
    saved = bench.journal.observations(last.attempt)
    assert record in saved and any(getattr(one, "traffic", None) is Traffic.LIVE for one in saved)
    cannot_qualify(bench, result, last)


def test_a_live_cell_cancelled_once_its_window_is_read_contributes_no_offline_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = Bench(tmp_path)
    low = next(one for one in PLAN.cells if one.identity.phase is Phase.LIVE_LOW_LOAD)
    read = telemetry.read

    def read_then_cancel(*args: Any, **kwargs: Any) -> Any:
        answers = read(*args, **kwargs)
        if running(bench) == low.identity.cell:
            bench.cancel.set()
        return answers

    monkeypatch.setattr(telemetry, "read", read_then_cancel)
    result = bench.run()
    cell = result.cells[-1]
    assert cell.cell == low and cell.reading is not None and not cell.checkpointed
    # Its trial passed, and is kept; graded offline, the interrupted cell counts none of it.
    assert [one.outcome for one in cell.trials] == [TrialOutcome.PASSED]
    records = quality.Records(result.manifest.workload, result.trials, tuple(one.record for one in result.evidence))
    graded_quality = quality.read(records, usd_per_host_hour=1)
    assert sum(one.successes for load in graded_quality.values() for one in load.cells) == 0
    cannot_qualify(bench, result, cell)


def test_a_cancellation_while_the_booking_probe_is_pending_stops_within_the_drain(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    sweep = next(one for one in PLAN.cells if one.identity.phase is Phase.CAPACITY_SWEEP)
    cancelled_at: list[int] = []

    async def slow() -> booking.Reason | None:
        # The probe takes 30 s to answer, well beyond the cell's drain.
        if running(bench) == sweep.identity.cell and not cancelled_at:
            await bench.clock.sleep_ms(30_000)
        return None

    def cancel(at: int, _student: int) -> None:
        if running(bench) == sweep.identity.cell and not cancelled_at:
            cancelled_at.append(at)
            bench.cancel.set()

    bench.continuity = slow
    bench.gateway.on_arrival = cancel
    result = bench.run()
    assert result.ended is phases.Ended.CANCELLED and cancelled_at
    assert not any(at > cancelled_at[0] + sweep.drain_ms for at, _ in bench.gateway.arrived)
    cannot_qualify(bench, result, result.cells[-1])


def test_a_cancellation_as_the_last_booking_probe_answers_interrupts_the_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = Bench(tmp_path)
    sweep = next(one for one in PLAN.cells if one.identity.phase is Phase.CAPACITY_SWEEP)
    read: list[bool] = []
    resolve = telemetry.resolve

    def resolved(*args: Any, **kwargs: Any) -> Any:
        reading = resolve(*args, **kwargs)
        if running(bench) == sweep.identity.cell:
            read.append(True)
        return reading

    async def continuity() -> booking.Reason | None:
        if read:
            bench.cancel.set()
        return None

    monkeypatch.setattr(telemetry, "resolve", resolved)
    bench.continuity = continuity
    result = bench.run()
    assert result.ended is phases.Ended.CANCELLED
    cell = result.cells[-1]
    assert cell.cell == sweep and cell.reading is not None
    cannot_qualify(bench, result, cell)


def test_a_cancellation_stops_the_route_warm_up_and_the_wait_for_scrapes(tmp_path: Path) -> None:
    routes = Bench(tmp_path / "routes")
    routes.gateway.on_arrival = lambda _at, _student: routes.cancel.set()
    result = routes.run()
    assert result.ended is phases.Ended.CANCELLED and len(routes.gateway.arrived) == 1
    assert not result.cells[0].checkpointed

    scrapes = Bench(tmp_path / "scrapes")
    sweep = next(one for one in PLAN.cells if one.identity.phase is Phase.CAPACITY_SWEEP)
    pending: list[asyncio.Task[None]] = []

    async def after_the_drain() -> None:
        await scrapes.clock.sleep_ms(sweep.admissions_ms + sweep.drain_ms + 1_000)
        scrapes.cancel.set()

    def cancel(_at: int, _student: int) -> None:
        if running(scrapes) == sweep.identity.cell and not pending:
            pending.append(asyncio.ensure_future(after_the_drain()))

    scrapes.gateway.on_arrival = cancel
    result = scrapes.run()
    assert result.ended is phases.Ended.CANCELLED
    last = result.cells[-1]
    assert last.cell == sweep and last.reading is None
    cannot_qualify(scrapes, result, last)


def test_a_continuity_probe_that_fails_stops_the_cell(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    sweep = next(one for one in PLAN.cells if one.identity.phase is Phase.CAPACITY_SWEEP)

    async def unreadable() -> booking.Reason | None:
        if running(bench) == sweep.identity.cell and bench.journal.attempts[-1].status is AttemptStatus.RUNNING:
            raise OSError("no answer")
        return None

    bench.continuity = unreadable
    result = bench.run()
    assert result.ended is phases.Ended.CONTINUITY_LOST and result.breach is booking.Reason.POOL_STATE_UNREADABLE
    last = result.cells[-1]
    assert last.cell == sweep
    cannot_qualify(bench, result, last)


class DriverStopped(Exception):
    pass


def test_requests_that_ended_stay_in_the_journal_when_the_driver_breaks(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    sweep = next(one for one in PLAN.cells if one.identity.phase is Phase.CAPACITY_SWEEP)
    sends: list[int] = []

    def lose_the_driver(at: int, _student: int) -> None:
        if running(bench) == sweep.identity.cell:
            sends.append(at)
            if len(sends) == 3:
                raise DriverStopped

    bench.gateway.on_arrival = lose_the_driver
    with pytest.raises(DriverStopped):
        bench.run()
    attempt = bench.journal.attempts[-1]
    assert attempt.cell == sweep.identity.cell and attempt.status is AttemptStatus.INTERRUPTED
    run_id = bench.journal.run_id
    bench.journal.close()
    with Journal.open(tmp_path / "runs", run_id) as reopened:
        saved = reopened.observations(attempt.ordinal)
    assert {getattr(one, "sent_ms", None) for one in saved} >= set(sends[:2])


def test_live_requests_stay_in_the_journal_when_the_worker_cleanup_fails(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    attempt = bench.harness.attempt
    measured: list[int] = []

    @contextlib.asynccontextmanager
    async def failing_cleanup(trial: Any, key: str, cancel: asyncio.Event) -> AsyncGenerator[trials.Work]:
        async with attempt(trial, key, cancel) as work:
            measured.extend(one.attempt.sent_ms for one in work.evidence.requests)
            yield work
        raise OSError("worker cleanup failed")

    bench.harness.attempt = failing_cleanup
    with pytest.raises(OSError, match="cleanup"):
        bench.run()
    last = bench.journal.attempts[-1]
    assert measured and last.status is AttemptStatus.INTERRUPTED
    saved = bench.journal.observations(last.ordinal)
    live = {one.sent_ms for one in saved if isinstance(one, RequestRecord) and one.traffic is Traffic.LIVE}
    assert set(measured) <= live


def test_a_resume_in_another_booking_warms_up_again_and_replaces_the_interrupted_trial(tmp_path: Path) -> None:
    first = Bench(tmp_path / "first")
    low = next(one for one in PLAN.cells if one.identity.phase is Phase.LIVE_LOW_LOAD)

    def cancel(trial: int, _turn: int) -> None:
        if trial in low.trials:
            first.cancel.set()

    first.harness.on_turn = cancel
    before = first.run()
    interrupted = before.trials[-1]
    assert interrupted.invalid is InvalidReason.INTERRUPTED

    second = Bench(tmp_path / "second", resumes=first.journal)
    second.clock.at = first.clock.at + 60_000
    after = second.run()
    assert after.ended is phases.Ended.COMPLETED
    # The new booking warmed its hosts again; the cells measured before were not run again.
    assert after.cells[0].cell.identity.phase is Phase.WARM_UP and after.cells[0].qualifies
    measured = {one.cell.identity.cell for one in before.cells[1:] if one.qualifies}
    assert measured and not measured & {one.cell.identity.cell for one in after.cells}
    assert all(one.qualifies for one in after.cells)
    # The replacement is an execution of its own, linked to the one it replaces.
    again = next(one for one in after.trials if one.trial == interrupted.trial)
    assert again.execution > interrupted.execution and again.replaces == interrupted.execution
    requests = [one.request for cells in (before.cells, after.cells) for cell in cells for one in cell.requests]
    assert len(requests) == len(set(requests))


def test_a_resume_on_another_engine_image_reuses_nothing_and_keeps_what_served_the_first(tmp_path: Path) -> None:
    first = Bench(tmp_path / "first")
    low = next(one for one in PLAN.cells if one.identity.phase is Phase.LIVE_LOW_LOAD)
    first.harness.on_turn = lambda trial, _turn: first.cancel.set() if trial in low.trials else None
    before = first.run()
    assert any(one.qualifies for one in before.cells[1:])

    second = Bench(tmp_path / "second", resumes=first.journal)
    second.clock.at = first.clock.at + 60_000
    other = "sha256:" + "be" * 32
    rows = second.workspace.series[telemetry.selectors(POOL, VLLM)[telemetry.Source.RELEASE]]
    rows[:] = [(labels | {"image_digest": other}, value, present) for labels, value, present in rows]
    after = second.run()
    assert after.ended is phases.Ended.SERVING_CHANGED
    (warm_up,) = after.cells
    assert warm_up.cell.identity.phase is Phase.WARM_UP and not warm_up.checkpointed
    saved = loads((first.journal.directory / phases.MANIFEST).read_text(), Manifest)
    assert saved.observed.engine_image_digest == IMAGE == after.manifest.observed.engine_image_digest


def test_a_booking_that_changes_interrupts_the_cell_and_starts_no_other(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    floor = next(
        one for one in PLAN.cells if plan.Role.CLASS_FLOOR in one.roles and one.identity.shape is Shape.HANDS_ON
    )

    def breach(at: int, student: int) -> None:
        # The second student first sends in the hands-on class-size cell: the booking changes then.
        if student == 1 and running(bench) == floor.identity.cell and bench.breach_at_ms is None:
            bench.breach_at_ms = at + 1

    bench.gateway.on_arrival = breach
    result = bench.run()
    assert result.ended is phases.Ended.CONTINUITY_LOST and result.breach is booking.Reason.BOOKING_CHANGED
    last = result.cells[-1]
    assert last.cell == floor and not last.checkpointed
    assert last.record is not None and last.record.status is CellStatus.INCOMPLETE
    cannot_qualify(bench, result, last)


def test_the_deadline_starts_no_cell_the_time_left_cannot_hold(tmp_path: Path) -> None:
    bench = Bench(tmp_path, deadline_ms=1_000_000)
    result = bench.run()
    assert result.ended is phases.Ended.DEADLINE
    assert 1 < len(result.cells) < len(PLAN.cells)
    assert all(one.checkpointed for one in result.cells)
    assert bench.clock.now_ms() <= 1_000_000
    left = PLAN.cells[len(result.cells)]
    reserved = [(one.identity.cell, PLAN.reserved_ms(one)) for one in PLAN.cells]
    resumed = bench.journal.resume(reserved, 10**9, PLAN.workload_hash, result.manifest.treatment_id)
    assert resumed.selected[0] == left.identity.cell
