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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

import common
from evaluate import booking, capacity, phases, plan, replay, telemetry, trials
from evaluate.contracts import (
    LIVE_PHASES,
    CellStatus,
    Grade,
    HostMetric,
    InstanceType,
    InvalidFlag,
    InvalidReason,
    Manifest,
    PathCategory,
    Phase,
    Profile,
    Route,
    Run,
    Settings,
    Shape,
    Suite,
    Traffic,
    TrialOutcome,
    loads,
)
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
    if route is Route.MESSAGES:
        usage = {"input_tokens": 30, "cache_read_input_tokens": cached}
        return [
            _event({"type": "message_start", "message": {"usage": usage}}, "message_start"),
            _event(
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}}, "content_block_delta"
            ),
            _event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}),
            _event({"type": "message_stop"}, "message_stop"),
        ]
    usage = {"input_tokens": 30, "output_tokens": 4}
    if route is Route.RESPONSES:
        done = {"type": "response.completed", "response": {"usage": usage}}
        return [_event({"type": "response.output_text.delta", "delta": "ok"}), _event(done, "response.completed")]
    return [
        _event({"choices": [{"index": 0, "delta": {"content": "ok"}}]}),
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
    each request arrived and from whom."""

    def __init__(self, clock: Clock, keys: Sequence[str]) -> None:
        self.clock, self.keys = clock, list(keys)
        self.arrived: list[tuple[int, int]] = []
        self.open = 0
        self.on_arrival: Callable[[int, int], None] = lambda _at, _student: None
        self._cached: set[tuple[int, str]] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        student = self.keys.index(request.headers["authorization"].removeprefix("Bearer "))
        self.arrived.append((self.clock.now_ms(), student))
        self.on_arrival(self.clock.now_ms(), student)
        body = json.loads(request.content)
        prefix = json.dumps([body.get("system"), body.get("instructions"), body.get("tools")])
        cached = 12 if (student, prefix) in self._cached else 0
        self._cached.add((student, prefix))
        events = _answer(_ROUTES[request.url.path], cached)

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
    up to now: two hosts that always work, and the gateway's count of each canary's requests.
    `change_at` replaces the second host by a third from that time on."""

    clock: Clock
    gateway: Gateway
    aliases: Sequence[str]
    change_at: float | None = None
    series: dict[str, list[tuple[dict[str, str], Callable[[float], float], Callable[[float], bool]]]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        held = telemetry.selectors(POOL, VLLM)
        always = lambda _t: True
        for host, present in (
            ("node-a", always),
            ("node-b", lambda t: self.change_at is None or t < self.change_at),
            ("node-c", lambda t: self.change_at is not None and t >= self.change_at),
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
            for key, rate in (
                (HostMetric.OUTPUT_TOKENS_TOTAL, 10),
                (HostMetric.INPUT_TOKENS_TOTAL, 100),
                (HostMetric.CACHED_INPUT_TOKENS_TOTAL, 50),
            ):
                self.add(
                    held[key],
                    {"instance": host, "engine": "0"},
                    lambda t, r=rate: 50 + r * (t - ORIGIN_S + 900),
                    present,
                )
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

    def count(self, student: int) -> Callable[[float], float]:
        def counted(at: float) -> float:
            return sum(1 for arrived, one in self.gateway.arrived if one == student and ORIGIN_S + arrived / 1000 < at)

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


@dataclass
class Harness:
    """A harness that sends its turns through the gateway as the live seat, then works a while
    unless it is told to stop. `silent` trials send nothing; `on_turn` sees each turn sent."""

    clock: Clock
    gateway: Gateway
    artifacts: Path
    silent: frozenset[int] = frozenset()
    on_turn: Callable[[int, int], None] = lambda _trial, _turn: None

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
                        received_ms=sent,
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
            tally=attempts.tally(),
            requests=tuple(measured),
        )
        yield trials.Work(evidence=evidence, artifacts=self.artifacts)


def grade_all(trial: Any, _work: trials.Work) -> Grade:
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

    def __post_init__(self) -> None:
        count = self.planned.canary_identities if self.keys is None else self.keys
        self.key_list = [generate_key() for _ in range(count)]
        self.gateway = Gateway(self.clock, self.key_list)
        alias_list = [f"canary-{at:02d}" for at in range(count)] if self.aliases is None else list(self.aliases)
        self.alias_list = alias_list
        self.workspace = Workspace(self.clock, self.gateway, alias_list)
        self.harness = Harness(self.clock, self.gateway, self.tmp_path)
        self.cancel = asyncio.Event()
        self.journal = Journal.create(self.tmp_path / "runs")
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
            pool=POOL,
            instances=2,
            starts_at=int(ORIGIN_S),
            ends_at=int(ORIGIN_S) + 6 * 3600,
            set_by="evaluate/run",
            set_at=int(ORIGIN_S),
        )
        outcome = booking.Outcome(reason=None, session=session, cleanup=booking.Cleanup.HELD)

        async def continuity() -> booking.Reason | None:
            breached = self.breach_at_ms is not None and self.clock.now_ms() >= self.breach_at_ms
            return booking.Reason.BOOKING_CHANGED if breached else None

        source = phases.Telemetry(self.workspace, POOL, VLLM, telemetry.Requested(REQUESTED, FINGERPRINT))
        return self.clock.drive(
            phases.run(
                self.planned,
                phases.Admitted(outcome, continuity),
                gateway,
                phases.Canaries(tuple(self.key_list), tuple(self.alias_list)),
                source,
                phases.Artifacts(RUN, trial_image(self.versions)),
                self.harness,
                dict.fromkeys(capacity_families(), grade_all),
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
    ],
    ids=["harness-version", "replay-fixture", "grader"],
)
def test_artifacts_that_are_not_the_plans_are_refused_before_measuring(
    tmp_path: Path, planned: plan.Plan, versions: dict[str, str]
) -> None:
    bench = Bench(tmp_path, planned=planned, versions=versions)
    assert bench.refused() is phases.Refusal.ARTIFACT_MISMATCH
    assert not (bench.journal.directory / phases.MANIFEST).exists()


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
        if student == 1 and bench.workspace.change_at is None:
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


def test_a_booking_that_changes_interrupts_the_cell_and_starts_no_other(tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    floor = next(
        one for one in PLAN.cells if plan.Role.CLASS_FLOOR in one.roles and one.identity.shape is Shape.HANDS_ON
    )

    def breach(at: int, student: int) -> None:
        # The second student first sends in the hands-on class-size cell: the booking changes then.
        if student == 1 and bench.breach_at_ms is None:
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
