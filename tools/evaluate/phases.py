"""Runs the measurement phases of a frozen plan in an admitted booking
(docs/design/agent-session-evaluation.md, "Booking and running" and "Workload and measurement
protocol").

`run` takes a `plan.Plan` and a booking that is ready (`Admitted`: a `booking.Outcome` and a probe
of its continuity) and runs the plan's cells in order, one at a time. It writes nothing of its own
to measure with; it binds what exists to each phase:

- the scheduler is `replay.run_cell`: the warm-up's synthetic sessions, the capacity sweep, the
  background of a live cell and the class openings;
- the tasks are `trials.run`, with the harnesses and graders it is given: the live cells at low
  load and at class size, one seat live and the others replaying;
- the telemetry reader is `telemetry.preflight`, `read` and `resolve`, over `Telemetry`'s reader;
- the journal is `journal.Journal`: each cell is an attempt that its observations are appended to
  and that ends checkpointed, or interrupted.

Who books, who owns the session and who publishes is the operator controller's: not this module.

**Before measuring.** Nothing is sent to the gateway until all of these hold, and each refusal is a
member of `Refusal`:

- the booking is ready (`booking-not-ready`);
- the canary identities are enough and distinct: the plan's keys (`replay.preflight`), each with a
  gateway alias of its own (`insufficient-identities`);
- the artifacts that will run are the ones the plan pins (`artifact-mismatch`): each harness's
  version as the trial image reports it (`image.installed`), each task instance's fixture and
  grader digests as its course module computes them now, and the digest of the course fixture the
  replay builds its requests from (`traces.COURSE`), which the plan's protocol digest covers. The
  run's `contracts.Manifest` is then written whole, before the first cell (`MANIFEST`);
- the telemetry a reading needs answers on every host the booking selected (`telemetry-unavailable`);
- the load driver has headroom: the plan's largest replay, driven on the run's clock against a
  local mock that answers at once, dispatches within the suite's tolerance at p95
  (`driver-headroom-missed`).

**The warm-up** is the plan's first cell. Its route warm-up is one streamed tool exchange on each
wire format of the suite (`warm_up.routes`), under the warm-up's own prefix; a route that does not
complete ends the run. Its per-host warm-up is the warm-up cell's replay, which sends until the
telemetry shows every selected host working beyond its liveness completions, and is then told it
has finished; one that does not get there within the suite's warm-up estimate ends the run. Its
window is the warm-up every later reading is given (`telemetry.Driver.warm_up`).

**Each cell** takes a baseline (`telemetry.preflight` at its start), is driven, waits on the run's
clock until the workspace holds the scrapes of its window (`telemetry.readable_from`), reads that
window and resolves it. Over the whole window it then checks:

- the identities: every seat the plan gives the cell sent as its own student, the live seat by its
  forwarder's requests and the replay seats by theirs;
- the cache conditions: the replay's fresh prefix was not left unverified, and each live attempt's
  first answer reported its cached input, so its starting cache is known from reported usage;
- the accounting: each scheduler's tally against its request rows, the forwarder's against its
  measurements, and the gateway's count of the cohort against every request the driver says
  reached it: the replay's, the route warm-up's and the live seat's, side queries included.

Each reason of the reading, and each failed check, becomes a `contracts.InvalidFlag` of the cell's
record. The requests, the trials and the host samples (`contracts.HostSample`, from the series the
window was read from) are appended to the cell's journal attempt, which is then checkpointed with
the record; `CellResult.evidence` is what `capacity.read` grades offline. Nothing is graded here.

**Stopping.** During every cell a watcher reads the cancel event, the deadline (the booking's end
less the suite's cleanup, or the run's own deadline if earlier) and the booking's continuity
(`Admitted.continuity`: the record still the booking, the same hosts, the same release). When one
trips, the cell's stop is set: the replay sends nothing more and cuts what it has open, the live
attempt is told to stop and is recorded invalid, `interrupted`, and what is still running after the
cell's drain (and, for a live cell, the worker's `forwarding.CANCEL_S`) is cancelled. What the cell
observed is still appended, and its attempt is interrupted, never checkpointed: its observations
stay readable (`Journal.observations`), it cannot qualify, and a resume runs it again. No cell
starts after that, nor one that the time left cannot hold (`plan.reserved_ms`). Why the run ended
is `Result.ended`, apart from any measurement.

Everything recorded is ordinals, run-clock times, counts and labels, as `evaluate.contracts` holds
them. It needs `spikes` and `services/gateway` on the path, as `evaluate.replay` does.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import math
import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx

import loadgen
from evaluate import booking, capacity, image, plan, replay, telemetry, traces, trials
from evaluate.contracts import (
    LIVE_PHASES,
    MAX_COUNT,
    MAX_TIME_MS,
    METRIC_UNITS,
    CellRecord,
    CellStatus,
    Family,
    Harness,
    HostMetric,
    HostSample,
    InvalidFlag,
    Manifest,
    Phase,
    RequestOutcome,
    RequestRecord,
    Route,
    Run,
    Settings,
    Traffic,
    TrialRecord,
    dumps,
)
from evaluate.course import analysis, optimization, research, revision
from evaluate.forwarding import CANCEL_S
from evaluate.streams import Attempts, Tally
from rhi_lifecycle.serving import PROBE_INTERVAL_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from common import Series
    from evaluate.contracts import TrialIdentity
    from evaluate.forwarding import Evidence
    from evaluate.journal import Journal
    from evaluate.plan import Plan, PlannedCell
    from s5_capacity import Reader

# The file of the run's journal directory the manifest is written to.
MANIFEST = "manifest.json"
# How long the driver check drives the largest replay against the local mock.
HEADROOM_MS = 30_000
# How often the watcher reads the booking's continuity, and the warm-up the hosts' work.
WATCH_MS = booking.POLL_SECONDS * 1000
WARM_UP_POLL_MS = telemetry.SCRAPE_SECONDS * 1000
# The tool the route warm-up declares, and what the exchange asks.
WARM_UP_TOOL = "warm_up_probe"
_SCHEMA = {"type": "object", "properties": {}}
_ASK = "Call the tool once."
_PATHS = {
    Route.MESSAGES: "/v1/messages",
    Route.RESPONSES: "/v1/responses",
    Route.CHAT_COMPLETIONS: "/v1/chat/completions",
}
# The course module that computes each family's fixture and grader digests.
COURSE_MODULES = {
    Family.ANALYSIS: analysis,
    Family.DEBUGGING: revision,
    Family.OPTIMIZATION: optimization,
    Family.RESEARCH: research,
}
# The SDK the SDK tool loop is pinned at.
SDK_TOOL_LOOP_SDK = "openai_sdk"


class Refusal(StrEnum):
    """Why nothing was measured."""

    BOOKING_NOT_READY = "booking-not-ready"
    INSUFFICIENT_IDENTITIES = "insufficient-identities"
    ARTIFACT_MISMATCH = "artifact-mismatch"
    TELEMETRY_UNAVAILABLE = "telemetry-unavailable"
    DRIVER_HEADROOM_MISSED = "driver-headroom-missed"


class PhaseError(Exception):
    """A run refused before anything was sent to the gateway, with its reason."""

    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class Ended(StrEnum):
    """Why the run stopped sending."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    CONTINUITY_LOST = "continuity-lost"
    ROUTE_WARM_UP_FAILED = "route-warm-up-failed"
    WARM_UP_INCOMPLETE = "warm-up-incomplete"


@dataclass(frozen=True)
class Canaries:
    """The cohort's canary keys, in seat order, and the gateway's roster alias of each, which
    selects its series at the gateway. Neither is ever recorded."""

    keys: tuple[str, ...] = field(repr=False)
    aliases: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class Telemetry:
    """Where a window is read: the workspace's reader, the pool and the engine row its series are
    named by, the treatment requested, and what the driver read of the tokenizer and the gateway's
    profile."""

    reader: Reader
    pool: str
    engine: str
    requested: telemetry.Requested
    tokenizer_revision: str | None = None
    gateway_profile_digest: str | None = None


@dataclass(frozen=True)
class Artifacts:
    """What runs the measurement: the run's record (its source, lock, images and driver), and a
    runner of commands in the trial image, which `image.installed` reads the versions with."""

    run: Run
    trial_image: image.Runner


@dataclass(frozen=True)
class Admitted:
    """A booking that `booking.Capacity` found ready, and the probe of its continuity: `None` while
    the record is still the booking, its hosts the same and its release the one expected, or the
    `booking.Reason` it is no longer so (`booking-changed`, `capacity-unavailable`,
    `release-mismatch`)."""

    outcome: booking.Outcome
    continuity: Callable[[], Awaitable[booking.Reason | None]]


@dataclass(frozen=True)
class CellResult:
    """What one cell left. `record` is `None` for a cell that stopped before it had one; a cell
    that was not checkpointed was interrupted, and its observations are in its journal attempt."""

    cell: PlannedCell
    attempt: int
    record: CellRecord | None
    requests: tuple[RequestRecord, ...]
    trials: tuple[TrialRecord, ...]
    samples: tuple[HostSample, ...]
    tally: Tally | None
    reading: telemetry.Reading | None
    checkpointed: bool

    @property
    def qualifies(self) -> bool:
        """Whether the cell can contribute a qualifying result: checkpointed and valid."""
        return self.checkpointed and self.record is not None and self.record.valid_for_qualification

    @property
    def evidence(self) -> capacity.CellEvidence | None:
        """The cell as `capacity.read` grades it: `None` with no record."""
        if self.record is None:
            return None
        served = {} if self.reading is None else {host.host: host.in_service_seconds for host in self.reading.hosts}
        return capacity.CellEvidence(self.record, self.requests, self.samples, self.tally, served)


@dataclass(frozen=True)
class Result:
    """The run: its manifest, every cell it started in order, and why it ended."""

    manifest: Manifest
    cells: tuple[CellResult, ...]
    ended: Ended
    breach: booking.Reason | None

    @property
    def evidence(self) -> tuple[capacity.CellEvidence, ...]:
        return tuple(one.evidence for one in self.cells if one.evidence is not None)

    @property
    def trials(self) -> tuple[TrialRecord, ...]:
        return tuple(record for one in self.cells for record in one.trials)

    def steady_state(self) -> tuple[RequestRecord, ...]:
        """The requests a steady-state summary may read: of the cells the plan marks steady, and
        none tagged warm-up. The warm-up's and the openings' are never among them."""
        return tuple(
            request
            for one in self.cells
            if one.cell.steady_state
            for request in one.requests
            if request.traffic is not Traffic.WARM_UP
        )


# --- Before measuring --------------------------------------------------------------------------


def installed_versions(run: image.Runner) -> dict[Harness, str]:
    """Each harness's version as the trial image reports it: the four npm harnesses by their
    `--version`, the SDK tool loop by its SDK's. `PhaseError` when the image cannot say."""
    try:
        found = image.installed(run)
    except (RuntimeError, ValueError, KeyError):
        raise PhaseError(Refusal.ARTIFACT_MISMATCH) from None
    versions = {Harness(name): version for name, version in found["harnesses"].items() if name in set(Harness)}
    return versions | {Harness.SDK_TOOL_LOOP: found["sdks"][SDK_TOOL_LOOP_SDK]}


def _nothing_observed() -> Settings:
    return Settings(**dict.fromkeys(spec.name for spec in dataclasses.fields(Settings)))


def resolve_manifest(plan_: Plan, artifacts: Artifacts, requested: Settings) -> Manifest:
    """The run's manifest from what will actually run, held to what the plan pins: the harnesses
    its trials run on, the fixture and grader of each instance they work on, and the course fixture
    the replay sends from. `PhaseError` (`artifact-mismatch`) for anything else."""
    versions = installed_versions(artifacts.trial_image)
    if any(versions.get(pin.harness) != pin.version for pin in plan_.workload.harnesses):
        raise PhaseError(Refusal.ARTIFACT_MISMATCH)
    worked = {trial.instance_id for trial in plan_.workload.trials}
    for task in plan_.spec.suite.tasks:
        module = COURSE_MODULES[task.family]
        for instance in task.instances:
            if instance.instance_id not in worked:
                continue
            computed = (module.fixture_digest(instance.instance_id), module.grader_digest(instance.instance_id))
            if computed != (instance.fixture_digest, instance.grader_digest):
                raise PhaseError(Refusal.ARTIFACT_MISMATCH)
    if plan_.spec.protocol.get("replay_fixture_digest") != plan.file_digest(traces.COURSE):
        raise PhaseError(Refusal.ARTIFACT_MISMATCH)
    return Manifest(workload=plan_.workload, requested=requested, run=artifacts.run, observed=_nothing_observed())


def _write_manifest(directory: Path, manifest: Manifest) -> None:
    """The manifest, written whole beside the journal: to a file beside it, then renamed."""
    beside = directory / (MANIFEST + ".tmp")
    descriptor = os.open(beside, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(dumps(manifest) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(beside, directory / MANIFEST)


def cohort(plan_: Plan, canaries: Canaries) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The keys the plan runs as, and their aliases: as many distinct keys as the plan needs
    (`replay.preflight`), each with an alias no other of them has. `PhaseError` otherwise."""
    if len(canaries.keys) != len(canaries.aliases):
        raise PhaseError(Refusal.INSUFFICIENT_IDENTITIES)
    try:
        (keys,) = replay.preflight(plan_, canaries.keys)
    except replay.ReplayError:
        raise PhaseError(Refusal.INSUFFICIENT_IDENTITIES) from None
    alias_of = dict(zip(canaries.keys, canaries.aliases, strict=True))
    aliases = tuple(alias_of[key] for key in keys)
    if len(set(aliases)) != len(aliases) or not all(aliases):
        raise PhaseError(Refusal.INSUFFICIENT_IDENTITIES)
    return keys, aliases


def _mock_answer(request: httpx.Request) -> httpx.Response:
    """The local mock of the driver check: a whole Messages stream, at once."""
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 1, "cache_read_input_tokens": 0}}}),
        ("content_block_delta", {"delta": {"type": "text_delta", "text": "ok"}}),
        ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
        ("message_stop", {}),
    ]
    body = b"".join(f"event: {name}\ndata: {json.dumps({'type': name} | data)}\n\n".encode() for name, data in events)
    return httpx.Response(200, headers={"content-type": loadgen.STREAM_TYPE}, content=body, request=request)


async def headroom(plan_: Plan, clock: replay.Clock, keys: Sequence[str], *, admissions_ms: int = HEADROOM_MS) -> bool:
    """Whether the load driver has headroom: the plan's largest replay, driven for `admissions_ms`
    on the run's clock against a local mock that answers at once, dispatched within the suite's
    tolerance at p95. Nothing is sent to the gateway."""
    largest = max(plan_.cells, key=lambda one: one.replay_slots)
    if not largest.replay_slots:
        return True
    rehearsal = dataclasses.replace(
        largest,
        identity=dataclasses.replace(largest.identity, phase=Phase.CAPACITY_SWEEP),
        admissions_ms=admissions_ms,
        drain_ms=0,
        arrivals_ms=(0,) * largest.replay_slots,
    )
    at = largest.identity.cell
    rehearsed = dataclasses.replace(plan_, cells=(*plan_.cells[:at], rehearsal, *plan_.cells[at + 1 :]))
    mock = replay.Gateway("https://driver-check.example.com", "driver-check", clock, httpx.MockTransport(_mock_answer))
    ran = await replay.run_cell(rehearsed, at, mock, keys)
    lag, tolerance = ran.summary.lag_p95_ms, ran.summary.lag_tolerance_ms
    return lag is not None and lag <= tolerance


# --- Waiting -------------------------------------------------------------------------------------


async def _first(*awaitables: Awaitable[object]) -> None:
    """Waits until one of them has ended; the others are cancelled and awaited."""
    futures = [asyncio.ensure_future(one) for one in awaitables]
    try:
        await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for one in futures:
            one.cancel()
        await asyncio.gather(*futures, return_exceptions=True)


async def _until(clock: replay.Clock, at_ms: int) -> None:
    while (wait := at_ms - clock.now_ms()) > 0:
        await clock.sleep_ms(wait)


# --- Host samples --------------------------------------------------------------------------------


def _usable(value: float) -> bool:
    return math.isfinite(value) and 0 <= value <= MAX_COUNT


def host_samples(
    answers: Mapping[telemetry.Key, Sequence[Series]],
    reading: telemetry.Reading,
    window: telemetry.Window,
    ordinals: telemetry.Ordinals,
    *,
    cell: int,
    origin_s: float,
) -> tuple[HostSample, ...]:
    """The samples of each host of the reading, one a metric and a scrape, from a scrape before
    the window's start to one after its drain, on the run's clock: the series of a host at one
    scrape summed, or averaged for a ratio. A host is the reading's ordinal of its instance."""
    hosts = {host.host for host in reading.hosts}
    low, high = window.started - telemetry.ALLOWANCE_SECONDS, window.drained + telemetry.ALLOWANCE_SECONDS
    found: list[HostSample] = []
    for metric in HostMetric:
        combine = telemetry.GAUGES.get(metric, sum)
        by: dict[tuple[int, int], list[float]] = {}
        for one in answers.get(metric, ()):
            instance = one.labels.get("instance", "")
            inside = any(window.started <= at <= window.drained for at, _ in one.samples)
            if not instance or not inside:
                continue
            host = ordinals.of(instance)
            for at, value in one.samples:
                at_ms = round((at - origin_s) * 1000)
                if host in hosts and low <= at <= high and _usable(value) and 0 <= at_ms <= MAX_TIME_MS:
                    by.setdefault((host, at_ms), []).append(value)
        for (host, at_ms), values in sorted(by.items()):
            value = combine(values)
            if metric is HostMetric.GPU_UTILIZATION and value > 1:
                continue
            found.append(
                HostSample(
                    cell=cell,
                    host=host,
                    at_ms=at_ms,
                    metric=metric,
                    unit=METRIC_UNITS[metric],
                    value=value,
                )
            )
    return tuple(found)


# --- Accounting ------------------------------------------------------------------------------------


def _tally(tallies: Iterable[Tally]) -> Tally:
    """Tallies summed, cause by cause."""
    held = list(tallies)
    return Tally(
        attempts=sum(one.attempts for one in held),
        unfinished=sum(one.unfinished for one in held),
        completed=sum(one.completed for one in held),
        failures={
            cause: sum(one.failures.get(cause, 0) for one in held)
            for cause in RequestOutcome
            if cause is not RequestOutcome.COMPLETED
        },
        retries=sum(one.retries for one in held),
        usage_missing=sum(one.usage_missing for one in held),
        eligible=sum(one.eligible for one in held),
    )


def _rows_tallied(tally: Tally, rows: int) -> bool:
    """Whether every send a tally counted ended and has its row."""
    return tally.unfinished == 0 and tally.attempts == rows


def _answered_first_reported(evidence: Evidence) -> bool:
    """Whether a live attempt's first answer reported its cached input: its starting cache is
    then known from reported usage."""
    answered = [one.attempt for one in evidence.requests if one.attempt.usage is not None or not one.attempt.failed]
    if not answered:
        return False
    first = min(answered, key=lambda attempt: attempt.sent_ms)
    return first.usage is not None and first.usage.cached_input_tokens is not None


def _liveness_tokens(seconds: float) -> int:
    """At most what a host's own liveness completions count over `seconds`, as telemetry reads it."""
    return int((seconds + 2 * telemetry.ALLOWANCE_SECONDS) // PROBE_INTERVAL_SECONDS) + 1


def _agreed(readings: Iterable[telemetry.Reading]) -> Settings:
    """What every reading observed alike, setting by setting; `None` where they differ or none did."""
    held = list(readings)
    agreed = {}
    for spec in dataclasses.fields(Settings):
        values = {getattr(one.observed, spec.name) for one in held}
        agreed[spec.name] = values.pop() if len(values) == 1 else None
    return Settings(**agreed) if held else _nothing_observed()


# --- The route warm-up ----------------------------------------------------------------------------


def route_exchange(route: Route, model: str, prefix: str) -> tuple[str, dict[str, str], bytes]:
    """One streamed tool exchange on a route: its path, headers and body, under `prefix`."""
    if route is Route.MESSAGES:
        body: dict[str, object] = {
            "model": model,
            "max_tokens": 16,
            "stream": True,
            "system": prefix,
            "tools": [{"name": WARM_UP_TOOL, "description": prefix, "input_schema": _SCHEMA}],
            "tool_choice": {"type": "any"},
            "messages": [{"role": "user", "content": _ASK}],
        }
        headers = dict(traces.HEADERS)
    elif route is Route.RESPONSES:
        body = {
            "model": model,
            "max_output_tokens": 16,
            "stream": True,
            "instructions": prefix,
            "tools": [{"type": "function", "name": WARM_UP_TOOL, "description": prefix, "parameters": _SCHEMA}],
            "tool_choice": "required",
            "input": _ASK,
        }
        headers = {"content-type": "application/json"}
    else:
        body = {
            "model": model,
            "max_tokens": 16,
            "stream": True,
            "stream_options": {"include_usage": True},
            "tools": [
                {"type": "function", "function": {"name": WARM_UP_TOOL, "description": prefix, "parameters": _SCHEMA}}
            ],
            "tool_choice": "required",
            "messages": [{"role": "system", "content": prefix}, {"role": "user", "content": _ASK}],
        }
        headers = {"content-type": "application/json"}
    return _PATHS[route], headers, json.dumps(body).encode()


# --- The run -------------------------------------------------------------------------------------


class _Recorded:
    """The harnesses, keeping what each attempt's forwarder observed, by its trial."""

    def __init__(self, inner: trials.Harnesses) -> None:
        self.inner = inner
        self.evidence: dict[int, Evidence] = {}

    @contextlib.asynccontextmanager
    async def attempt(self, trial: TrialIdentity, key: str, cancel: asyncio.Event) -> AsyncGenerator[trials.Work]:
        async with self.inner.attempt(trial, key, cancel) as work:
            self.evidence[trial.trial] = work.evidence
            yield work


@dataclass
class _Driven:
    """What driving a cell left, before its telemetry is read."""

    record: CellRecord | None
    requests: list[RequestRecord]
    trials: list[TrialRecord]
    tallies: list[Tally]
    live: list[Evidence]
    flags: set[InvalidFlag]
    side_queries: int = 0


class _Run:
    def __init__(
        self,
        plan_: Plan,
        admitted: Admitted,
        gateway: replay.Gateway,
        keys: tuple[str, ...],
        aliases: tuple[str, ...],
        source: Telemetry,
        harnesses: trials.Harnesses,
        graders: Mapping[Family, trials.Grader],
        journal: Journal,
        manifest: Manifest,
        cancel: asyncio.Event,
        origin_s: float,
        deadline_ms: int,
    ) -> None:
        self.plan, self.admitted, self.gateway, self.keys, self.aliases = plan_, admitted, gateway, keys, aliases
        self.source, self.harnesses, self.graders, self.journal = source, _Recorded(harnesses), graders, journal
        self.manifest, self.cancel, self.origin_s, self.deadline_ms = manifest, cancel, origin_s, deadline_ms
        self.clock = gateway.clock
        self.ordinals = telemetry.Ordinals()
        self.evidence = replay.Evidence()
        self.next_request = 0
        self.execution = 0
        self.warm_up: telemetry.Window | None = None
        self.ended: Ended | None = None
        self.breach: booking.Reason | None = None
        session = admitted.outcome.session
        self.hosts = 0 if session is None else session.instances

    # Times.

    def epoch(self, at_ms: int) -> float:
        return self.origin_s + at_ms / 1000

    def window(self, started_ms: int, ended_ms: int) -> telemetry.Window:
        return telemetry.Window(self.epoch(started_ms), self.epoch(max(ended_ms, started_ms + 1)))

    # Stopping.

    async def _tripped(self) -> Ended | None:
        if self.cancel.is_set():
            return Ended.CANCELLED
        if self.clock.now_ms() >= self.deadline_ms:
            return Ended.DEADLINE
        breach = await self.admitted.continuity()
        if breach is not None:
            self.breach = breach
            return Ended.CONTINUITY_LOST
        return None

    async def _watch(self, stop: asyncio.Event) -> None:
        """Sets `stop` once the run is cancelled, past its deadline, or out of its booking."""
        while not stop.is_set():
            tripped = await self._tripped()
            if tripped is not None:
                self.ended = self.ended or tripped
                stop.set()
                return
            wake = min(self.clock.now_ms() + WATCH_MS, self.deadline_ms)
            await _first(_until(self.clock, wake), self.cancel.wait(), stop.wait())

    async def _bounded[T](self, work: Awaitable[T], stop: asyncio.Event, drain_ms: int) -> T | None:
        """Runs a cell's work under the watcher. Once it is stopped, the work has `drain_ms` to end
        and is then cancelled: `None` for work that had to be."""
        task = asyncio.ensure_future(work)
        watch = asyncio.ensure_future(self._watch(stop))
        try:
            await _first(asyncio.shield(task), stop.wait())
            if not task.done():
                await _first(asyncio.shield(task), _until(self.clock, self.clock.now_ms() + drain_ms))
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return None
            return task.result()
        finally:
            task.cancel()
            watch.cancel()
            await asyncio.gather(task, watch, return_exceptions=True)

    # Reading the telemetry.

    def _baseline(self) -> telemetry.Preflight:
        source = self.source
        return telemetry.preflight(source.reader, source.pool, source.engine, self.epoch(self.clock.now_ms()))

    def _driver(
        self, attempts: int, side_queries: int, warm_up: telemetry.Window | None, baseline: telemetry.Preflight | None
    ) -> telemetry.Driver:
        return telemetry.Driver(
            cohort=self.aliases,
            attempts=attempts,
            side_queries=side_queries,
            warm_up=warm_up,
            preflight=baseline,
            tokenizer_revision=self.source.tokenizer_revision,
            gateway_profile_digest=self.source.gateway_profile_digest,
        )

    async def _observe(
        self, cell: int, window: telemetry.Window, driver: telemetry.Driver
    ) -> tuple[telemetry.Reading, tuple[HostSample, ...]]:
        """Waits for the window's scrapes to be delivered, then reads and resolves it."""
        await _until(self.clock, math.ceil((telemetry.readable_from(window) - self.origin_s) * 1000))
        source = self.source
        answers = telemetry.read(source.reader, source.pool, source.engine, window, driver.warm_up)
        reading = telemetry.resolve(answers, window, source.requested, driver, self.ordinals)
        samples = host_samples(answers, reading, window, self.ordinals, cell=cell, origin_s=self.origin_s)
        return reading, samples

    def _hosts_working(self, started_ms: int) -> bool:
        """Whether, from `started_ms` to the last delivered scrape, every selected host counted
        output beyond its own liveness completions."""
        now = self.epoch(self.clock.now_ms())
        drained = now - telemetry.ALLOWANCE_SECONDS
        if drained <= self.epoch(started_ms):
            return False
        window = telemetry.Window(self.epoch(started_ms), drained)
        source = self.source
        answers = telemetry.read(source.reader, source.pool, source.engine, window)
        reading = telemetry.resolve(answers, window, source.requested, self._driver(0, 0, None, None), self.ordinals)
        floor = _liveness_tokens(window.seconds)
        worked = [
            host
            for host in reading.hosts
            if (output := host.counters[HostMetric.OUTPUT_TOKENS_TOTAL].increase) is not None and output > floor
        ]
        return len(worked) >= self.hosts and len(reading.hosts) == len(worked)

    # The cells.

    async def run(self, cells: Sequence[int]) -> list[CellResult]:
        done: list[CellResult] = []
        for cell in cells:
            if self.ended is not None:
                break
            planned = self.plan.cells[cell]
            if any(one.cell == cell and one.reusable for one in self.journal.attempts):
                continue
            tripped = await self._tripped()
            if tripped is None and self.clock.now_ms() + self.plan.reserved_ms(planned) > self.deadline_ms:
                tripped = Ended.DEADLINE
            if tripped is not None:
                self.ended = tripped
                break
            done.append(await self.cell(planned))
        self.ended = self.ended or Ended.COMPLETED
        return done

    async def cell(self, planned: PlannedCell) -> CellResult:
        number = planned.identity.cell
        attempt = self.journal.start_cell(number, self.plan.workload_hash, self.manifest.treatment_id).ordinal
        baseline = self._baseline()
        started_ms = self.clock.now_ms()
        stop = asyncio.Event()
        if planned.identity.phase is Phase.WARM_UP:
            driven = await self._warm_up_cell(planned, stop)
        elif planned.identity.phase in LIVE_PHASES:
            driven = await self._live_cell(planned, stop)
        else:
            driven = await self._replay_cell(planned, stop)
        for request in driven.requests:
            self.journal.append(attempt, request)
        for trial in driven.trials:
            self.journal.append(attempt, trial)
        tally = _tally(driven.tallies) if driven.tallies else None
        record, reading, samples = driven.record, None, ()
        if record is not None and self.ended is None:
            started_ms = min(started_ms, record.started_ms)
            window = self.window(started_ms, record.ended_ms)
            warm_up = None if planned.identity.phase is Phase.WARM_UP else self.warm_up
            attempts = len(driven.requests) - driven.side_queries
            reading, samples = await self._observe(
                number, window, self._driver(attempts, driven.side_queries, warm_up, baseline)
            )
            record = self._judged(planned, record, driven, reading, baseline, started_ms)
            for sample in samples:
                self.journal.append(attempt, sample)
            if planned.identity.phase is Phase.WARM_UP:
                self.warm_up = window
        checkpointed = record is not None and reading is not None
        if checkpointed and record is not None:
            self.journal.checkpoint_cell(attempt, record)
        else:
            self.journal.interrupt(attempt)
        return CellResult(
            cell=planned,
            attempt=attempt,
            record=record,
            requests=tuple(driven.requests),
            trials=tuple(driven.trials),
            samples=samples,
            tally=tally,
            reading=reading,
            checkpointed=checkpointed,
        )

    def _judged(
        self,
        planned: PlannedCell,
        record: CellRecord,
        driven: _Driven,
        reading: telemetry.Reading,
        baseline: telemetry.Preflight,
        started_ms: int,
    ) -> CellRecord:
        """The cell's record with every flag the whole window gives it."""
        flags = set(record.invalid_flags) | driven.flags
        reasons = set(reading.reasons)
        if planned.identity.phase is Phase.WARM_UP:
            # The warm-up is what warms: it does not owe a warm-up of its own.
            reasons.discard(telemetry.Reason.WARM_UP_INCOMPLETE)
        flags |= {telemetry.FLAG_OF[reason] for reason in reasons}
        if baseline.missing or baseline.hosts < self.hosts:
            flags.add(InvalidFlag.MISSING_EVIDENCE)
        seats = {one.student for one in driven.requests if one.traffic is not Traffic.LIVE}
        seats |= {trials.LIVE_STUDENT} if any(one.traffic is Traffic.LIVE for one in driven.requests) else set()
        if len(seats) < planned.identities:
            flags.add(InvalidFlag.MISSING_EVIDENCE)
        if not all(_answered_first_reported(one) for one in driven.live):
            flags.add(InvalidFlag.MISSING_EVIDENCE)
        return dataclasses.replace(
            record,
            started_ms=started_ms,
            invalid_flags=tuple(sorted(flags)),
            hosts_observed=len(reading.hosts),
        )

    def _replay_flags(self, ran: replay.CellRun) -> set[InvalidFlag]:
        flags: set[InvalidFlag] = set()
        if ran.summary.fresh_prefix is replay.Fresh.UNVERIFIED:
            flags.add(InvalidFlag.MISSING_EVIDENCE)
        if not _rows_tallied(ran.summary.tally, len(ran.requests)):
            flags.add(InvalidFlag.UNRECONCILED_COUNTERS)
        return flags

    def _took(self, requests: Iterable[RequestRecord]) -> None:
        self.next_request = max((one.request + 1 for one in requests), default=self.next_request)

    async def _replay_cell(self, planned: PlannedCell, stop: asyncio.Event) -> _Driven:
        work = replay.run_cell(
            self.plan,
            planned.identity.cell,
            self.gateway,
            self.keys,
            stops=replay.Stops(cancel=stop, deadline_ms=self.deadline_ms),
            first_request=self.next_request,
            evidence=self.evidence,
        )
        ran = await self._bounded(work, stop, planned.drain_ms)
        if ran is None:
            return _Driven(None, [], [], [], [], set())
        self._took(ran.requests)
        return _Driven(ran.record, list(ran.requests), [], [ran.summary.tally], [], self._replay_flags(ran))

    async def _route_warm_up(self, planned: PlannedCell) -> tuple[list[RequestRecord], Tally]:
        """One streamed tool exchange on each route of the suite, as the cohort's first student,
        under the warm-up's prefix."""
        attempts = Attempts()
        records: list[RequestRecord] = []
        gateway, clock = self.gateway, self.clock
        prefix = f"warm-up {planned.input_seed} namespace {planned.prefix_namespace}"
        async with httpx.AsyncClient(
            base_url=gateway.endpoint, timeout=loadgen.TIMEOUT, transport=gateway.transport, trust_env=False
        ) as http:
            for conversation, name in enumerate(self.plan.spec.protocol["warm_up"]["routes"]):
                route = Route(name)
                path, headers, body = route_exchange(route, gateway.model, f"{prefix} route {route.value}")
                headers |= {"authorization": f"Bearer {self.keys[0]}"}
                eligible = sent = clock.now_ms()
                reader = attempts.send(route, sent)
                try:
                    async with http.stream("POST", path, headers=headers, content=body) as answer:
                        reader.answered(answer.status_code)
                        async for chunk in answer.aiter_bytes():
                            reader.feed(chunk, clock.now_ms())
                except httpx.HTTPError:
                    reader.end(clock.now_ms(), transport_error=True)
                ended = reader.end(clock.now_ms())
                records.append(
                    RequestRecord(
                        request=self.next_request,
                        cell=planned.identity.cell,
                        student=0,
                        conversation=conversation,
                        turn=0,
                        traffic=Traffic.WARM_UP,
                        trial=None,
                        eligible_ms=eligible,
                        finish_reason=None,
                        **ended.measured(),
                    )
                )
                self.next_request += 1
        return records, attempts.tally()

    async def _warm_up_cell(self, planned: PlannedCell, stop: asyncio.Event) -> _Driven:
        started_ms = self.clock.now_ms()
        routes, tally = await self._route_warm_up(planned)
        if any(one.failed for one in routes):
            self.ended = Ended.ROUTE_WARM_UP_FAILED
            return _Driven(None, routes, [], [tally], [], set())
        finished = asyncio.Event()
        until_ms = min(self.deadline_ms, started_ms + self.plan.spec.estimates.warm_up_ms)
        work = replay.run_cell(
            self.plan,
            planned.identity.cell,
            self.gateway,
            self.keys,
            stops=replay.Stops(finished=finished, cancel=stop, deadline_ms=until_ms),
            first_request=self.next_request,
            evidence=self.evidence,
            first_conversation=len(routes),
        )

        async def poll() -> None:
            while not finished.is_set():
                await _until(self.clock, self.clock.now_ms() + WARM_UP_POLL_MS)
                if self._hosts_working(started_ms):
                    finished.set()

        polling = asyncio.ensure_future(poll())
        try:
            ran = await self._bounded(work, stop, planned.drain_ms)
        finally:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)
        if ran is None:
            return _Driven(None, routes, [], [tally], [], set())
        self._took(ran.requests)
        if not finished.is_set() and self.ended is None:
            self.ended = Ended.WARM_UP_INCOMPLETE
        record = dataclasses.replace(ran.record, started_ms=started_ms)
        requests = [*routes, *ran.requests]
        return _Driven(record, requests, [], [tally, ran.summary.tally], [], self._replay_flags(ran))

    async def _live_cell(self, planned: PlannedCell, stop: asyncio.Event) -> _Driven:
        self.harnesses.evidence.clear()
        work = trials.run(
            self.plan,
            self.gateway,
            self.keys,
            self.harnesses,
            self.graders,
            cells=[planned.identity.cell],
            cancel=stop,
            evidence=self.evidence,
            first_execution=self.execution,
            first_request=self.next_request,
        )
        ran = await self._bounded(work, stop, planned.drain_ms + round(CANCEL_S * 1000))
        if ran is None or not ran.cells:
            return _Driven(None, [], [], [], [], set())
        (cell,) = ran.cells
        self.execution += len(cell.trials)
        background = cell.background
        requests = list(background.requests)
        self._took(requests)
        flags: set[InvalidFlag] = set()
        for one in background.runs:
            flags |= self._replay_flags(one)
        tallies = [one.summary.tally for one in background.runs]
        conversation = max((one.conversation + 1 for one in requests), default=0)
        live: list[Evidence] = []
        side = 0
        for at, executed in enumerate(cell.trials):
            evidence = self.harnesses.evidence.get(executed.record.trial)
            if evidence is None:
                continue
            live.append(evidence)
            tallies.append(evidence.tally)
            if not _rows_tallied(evidence.tally, len(evidence.requests)):
                flags.add(InvalidFlag.UNRECONCILED_COUNTERS)
            side += sum(one.side_call for one in evidence.requests)
            for turn, measured in enumerate(evidence.requests):
                requests.append(
                    measured.request_record(
                        request=self.next_request,
                        cell=planned.identity.cell,
                        student=trials.LIVE_STUDENT,
                        conversation=conversation + at,
                        turn=turn,
                        trial=executed.record.trial,
                    )
                )
                self.next_request += 1
        record = background.record
        counted = {one.record.trial for one in cell.trials if one.record.counts_as_attempt}
        if record is not None and counted != set(planned.trials):
            record = dataclasses.replace(record, status=CellStatus.INCOMPLETE)
        trial_records = [one.record for one in cell.trials]
        return _Driven(record, requests, trial_records, tallies, live, flags, side_queries=side)


async def run(
    plan_: Plan,
    admitted: Admitted,
    gateway: replay.Gateway,
    canaries: Canaries,
    source: Telemetry,
    artifacts: Artifacts,
    harnesses: trials.Harnesses,
    graders: Mapping[Family, trials.Grader],
    journal: Journal,
    *,
    origin_s: float,
    cells: Sequence[int] | None = None,
    cancel: asyncio.Event | None = None,
    deadline_ms: int | None = None,
    headroom_ms: int = HEADROOM_MS,
) -> Result:
    """Runs the plan's cells in order in the admitted booking (the module's text): all of them
    unless `cells` names some, the warm-up first in any case. `origin_s` is the time, in seconds
    since the epoch, at which the run's clock (`gateway.clock`) read zero. `PhaseError`, with
    nothing sent to the gateway, for a run that cannot be measured."""
    session = admitted.outcome.session
    if not admitted.outcome.ready or session is None:
        raise PhaseError(Refusal.BOOKING_NOT_READY)
    keys, aliases = cohort(plan_, canaries)
    manifest = resolve_manifest(plan_, artifacts, source.requested.settings)
    _write_manifest(journal.directory, manifest)
    clock = gateway.clock
    found = telemetry.preflight(source.reader, source.pool, source.engine, origin_s + clock.now_ms() / 1000)
    if found.missing or found.hosts < session.instances:
        raise PhaseError(Refusal.TELEMETRY_UNAVAILABLE)
    if not await headroom(plan_, clock, keys, admissions_ms=headroom_ms):
        raise PhaseError(Refusal.DRIVER_HEADROOM_MISSED)
    booked_ms = round((session.ends_at - origin_s) * 1000) - plan_.spec.estimates.cleanup_ms
    ends_ms = booked_ms if deadline_ms is None else min(booked_ms, deadline_ms)
    runner = _Run(
        plan_,
        admitted,
        gateway,
        keys,
        aliases,
        source,
        harnesses,
        graders,
        journal,
        manifest,
        asyncio.Event() if cancel is None else cancel,
        origin_s,
        ends_ms,
    )
    chosen = [one.identity.cell for one in plan_.cells] if cells is None else [0, *(one for one in cells if one)]
    done = await runner.run(chosen)
    readings = [one.reading for one in done if one.reading is not None]
    manifest = dataclasses.replace(manifest, observed=_agreed(readings))
    _write_manifest(journal.directory, manifest)
    return Result(manifest=manifest, cells=tuple(done), ended=runner.ended or Ended.COMPLETED, breach=runner.breach)


__all__ = [
    "Admitted",
    "Artifacts",
    "Canaries",
    "CellResult",
    "Ended",
    "PhaseError",
    "Refusal",
    "Result",
    "Telemetry",
    "run",
]
