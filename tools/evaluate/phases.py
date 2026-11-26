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
- the telemetry reader is `telemetry.preflight`, `read`, `resolve` and `identity_requests`, over
  `Telemetry`'s reader;
- the journal is `journal.Journal`: each cell is an attempt that its observations are appended to
  as they are made, and that ends checkpointed, or interrupted.

Who books, who owns the session and who publishes is the operator controller's: not this module.

**Before measuring.** Nothing is sent to the gateway until all of these hold, and each refusal is a
member of `Refusal`:

- the booking is ready (`booking-not-ready`), and the gateway's pool and the telemetry's are the
  pool it booked (`booking-mismatch`);
- the canary identities are enough and distinct: the plan's keys (`replay.preflight`), each with a
  gateway alias of its own (`insufficient-identities`);
- the artifacts that will run are the ones the plan pins (`artifact-mismatch`): each harness's
  version as the trial image reports it (`image.installed`), for every family the selected live
  cells run the course's own grader (`trials.course_graders`), the one the workload's grader
  digests identify, each task instance's fixture and grader digests as its course module computes
  them now, the digest of the course fixture the replay builds its requests from (`traces.COURSE`),
  and the suite, arrival and protocol digests computed again from the spec and the cells that will
  be sent (`plan.digests_hold`);
- a journal that holds earlier cells holds them under this workload and this treatment, and so
  does the manifest beside it, whose run record (its source, lock, harness and driver images) is
  this run's (`incompatible-resume`): a run is resumed, never overwritten, and what it keeps stays
  attributed to what made it. What served the measurements it keeps is in `SERVED`;
- the telemetry a reading needs answers on every host the booking selected, and on no other host
  (`telemetry-unavailable`, `booking-mismatch`);
- the load driver has headroom: the plan's largest replay, driven on the run's clock against a
  local mock that answers at once, dispatches within the suite's tolerance at p95
  (`driver-headroom-missed`).

The run's `contracts.Manifest` is then written whole (`MANIFEST`), before the first cell.

**The warm-up** is the plan's first cell, and every booking runs it again, even when an earlier
booking's warm-up is in the journal: a warm-up warms the engines that did it. Its route warm-up is
one streamed tool exchange on each wire format of the suite (`warm_up.routes`), under the warm-up's
own prefix; a route whose answer does not complete with a call of the declared tool ends the run.
Its per-host warm-up is the warm-up cell's replay, widened to one seat for each canary of the
cohort, so that each host a student's gateway affinity sends to is reached. It sends until the
telemetry shows every selected host working beyond its liveness completions, and is then told it
has finished; one that does not get there within the suite's warm-up estimate ends the run. Its
window is the warm-up every later reading is given (`telemetry.Driver.warm_up`).

**Each cell** takes a baseline (`telemetry.preflight` at its start), is driven, waits on the run's
clock until the workspace holds the scrapes of its window (`telemetry.readable_from`), reads that
window and resolves it. Over the whole window it then checks:

- the hosts, the release and the engines' launch: the hosts the window shows are as many as the
  booking selected, and the reading's `hosts`, `release` and `launch_settings` treatments are not
  contradicted: a release or a launch fingerprint that changed, or a host count other than the
  requested one, flags the cell `host_turnover`, and a part the telemetry does not show flags it
  `missing_evidence`. Declared or not, every host served the same instance type, image, model
  revision and launch fingerprint through the window (`served_alike`), and what identifies what
  served (`SERVING`, and the launch fingerprint) is what served the warm-up, flagged the same way
  when it is not;
- the identities: every seat the cell drives sent as its own student, and the gateway's count of
  each canary's alias over the window is the number of requests the driver sent as that student;
- the cache conditions: the replay's fresh prefix was not left unverified, and each live attempt's
  first answers under the task's prefix, those sent before the first of them ended, reported a
  cached input of exactly zero (`replay.met_fresh`): a side query has a prefix of its own;
- the accounting: each scheduler's tally against its request rows, request by request, outcome
  by outcome and in the streams eligible for a speed, as `capacity.read` gives them one; the
  forwarder's against its measurements, the driver's dispatch lag of the live
  requests within the suite's tolerance at p95, and the gateway's count of the cohort against
  every request the driver says reached it: the replay's, the route warm-up's and the live seat's,
  side queries included.

Each reason of the reading, and each failed check, becomes a `contracts.InvalidFlag` of the cell's
record. Requests and trials are appended to the cell's journal attempt as each one ends; the host
samples (`contracts.HostSample`, from the series the window was read from) once the window is read.
A counter's samples are the increase each of its series counted since the first sample of the
window's range, summed over the host's series at each scrape: what the reading counted is what
`capacity.read` counts from them, whatever the counter's lifetime total and whichever of its
series restarted. The attempt is then checkpointed with the record. A record valid for
qualification also writes the manifest again, with what every such window observed alike, and,
past the warm-up, what served it to `SERVED`; an invalid one's observations stay in its attempt,
for diagnosis, and say nothing of what served the run. `CellResult.evidence` is what
`capacity.read` and `quality.read` grade offline. Nothing is graded here.

**Stopping.** From the moment a cell's attempt opens until its checkpoint is decided, a watcher
reads the cancel event, the deadline (the booking's end less the suite's cleanup, or the run's own
deadline if earlier) and the booking's continuity (`Admitted.continuity`: the record still the
booking, the same hosts, the same release); a probe that cannot answer is a continuity that cannot
be established (`pool-state-unreadable`). A pending probe does not hold the other two back: a
cancellation or the deadline cuts it short, and the stop is set without waiting for the probe's
cleanup, which the run awaits once its traffic has stopped; both are read again once a probe has
answered. When one trips, the cell's stop is set: no route and no
replay request is sent after it and the replay cuts what it has open, the live attempt is told to
stop and is recorded invalid, `interrupted`, and what is still running after the cell's drain (and,
for a live cell, the worker's `forwarding.CANCEL_S`) is cancelled. A stop while the window's
scrapes are awaited stops the cell as well, and so does a continuity that no longer holds once they
are read. What the cell observed stays in its attempt, which is interrupted, never checkpointed:
its observations stay readable (`Journal.observations`), its record, if it has one, is
`incomplete`, so that it cannot qualify, offline either, and a resume runs it again; a driver that
raises interrupts it too, and a live attempt that raises before it is graded (its worker's cleanup
failed) leaves the requests its forwarder measured in the attempt all the same. No cell starts after that, nor one that the time
left cannot hold (`plan.reserved_ms`). Why the run ended is `Result.ended`, apart from any
measurement.

**Resuming.** Request ordinals and trial executions go on from the highest the journal holds, and
an execution of a trial the journal already holds one of names the latest in `replaces`. The
measurements kept are reused only when the new booking's warm-up shows what served them (`SERVING`
and the launch fingerprint, as `SERVED` holds them); otherwise the run ends `serving-changed`,
before any other cell, and the manifest and `SERVED` keep what served them. A cell checkpointed
invalid is run again on a booking that matches.

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
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import httpx

import loadgen
from common import percentile
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
    loads,
)
from evaluate.course import analysis, optimization, research, revision
from evaluate.forwarding import CANCEL_S
from evaluate.journal import AttemptStatus, JournalError
from evaluate.streams import Attempts, Tally
from rhi_gateway.sse import SseReader
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
# The treatments a window must not contradict: what the booking selected and what serves it.
BOOKED = (telemetry.Treatment.HOSTS, telemetry.Treatment.RELEASE, telemetry.Treatment.LAUNCH_SETTINGS)
# What identifies what served a window, of what it observed: the same through the run, and through
# the measurements a resumed run keeps.
SERVING = ("host_count", "instance_type", "engine_image_digest", "model_revision")
# The launch fingerprint, with them; and the file of the journal's directory that holds what
# served the measurements a resume keeps.
LAUNCH = "launch_fingerprint"
SERVED = "served.json"
_UNSEEN = (telemetry.Reason.NOT_OBSERVED, telemetry.Reason.INCOMPLETE_COVERAGE)


class Refusal(StrEnum):
    """Why nothing was measured."""

    BOOKING_NOT_READY = "booking-not-ready"
    BOOKING_MISMATCH = "booking-mismatch"
    INSUFFICIENT_IDENTITIES = "insufficient-identities"
    ARTIFACT_MISMATCH = "artifact-mismatch"
    INCOMPATIBLE_RESUME = "incompatible-resume"
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
    SERVING_CHANGED = "serving-changed"


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


# Told of each attempt about to be checkpointed: its ordinal, its scheduler's tally, its reading.
type Keep = Callable[[int, Tally | None, telemetry.Reading], None]


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
    its trials run on, the fixture and grader of each instance they work on, the course fixture
    the replay sends from, and the workload the spec and the cells would send now. `PhaseError`
    (`artifact-mismatch`) for anything else."""
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
    if plan_.spec.replay_fixture_digest != plan.file_digest(traces.COURSE):
        raise PhaseError(Refusal.ARTIFACT_MISMATCH)
    if not plan.digests_hold(plan_):
        raise PhaseError(Refusal.ARTIFACT_MISMATCH)
    return Manifest(workload=plan_.workload, requested=requested, run=artifacts.run, observed=_nothing_observed())


def _graders_held(plan_: Plan, cells: Iterable[int], graders: Mapping[Family, trials.Grader]) -> None:
    """Refuses a run whose selected live cells run a family it has no grader for, or a grader other
    than the course's (`trials.course_graders`): the one whose digest the manifest's workload
    records, and the only one whose grades it identifies."""
    needed = {
        trials.task_of(plan_, plan_.workload.trials[trial]).family
        for cell in cells
        if plan_.cells[cell].identity.phase in LIVE_PHASES
        for trial in plan_.cells[cell].trials
    }
    course = trials.course_graders()
    for family in needed:
        given = graders.get(family)
        code = getattr(given, "__code__", None)
        if code is None or code is not getattr(course[family], "__code__", None):
            raise PhaseError(Refusal.ARTIFACT_MISMATCH)


def _resumable(plan_: Plan, journal: Journal, manifest: Manifest) -> tuple[Settings, Serving] | None:
    """Refuses a journal that holds cells, or a manifest, of another workload or treatment, or
    observations made with another run record (its images, its driver): the manifest the journal
    holds attributes them, and is not rewritten under them. Gives what the manifest observed of
    the measurements a resume keeps, and what served them (`SERVED`): `None` when it keeps none.
    A journal that keeps measurements without both is refused."""
    saved = journal.directory / MANIFEST
    before: Manifest | None = None
    if saved.is_file():
        try:
            before = loads(saved.read_text(encoding="utf-8"), Manifest)
        except ValueError:
            raise PhaseError(Refusal.INCOMPATIBLE_RESUME) from None
        if (before.workload_id, before.treatment_id) != (manifest.workload_id, manifest.treatment_id):
            raise PhaseError(Refusal.INCOMPATIBLE_RESUME)
        if journal.attempts and before.run != manifest.run:
            raise PhaseError(Refusal.INCOMPATIBLE_RESUME)
    try:
        journal.resume([(one.identity.cell, 0) for one in plan_.cells], 0, manifest.workload_id, manifest.treatment_id)
    except JournalError:
        raise PhaseError(Refusal.INCOMPATIBLE_RESUME) from None
    if not any(one.reusable and plan_.cells[one.cell].identity.phase is not Phase.WARM_UP for one in journal.attempts):
        return None
    served = _load_served(journal.directory)
    if before is None or served is None:
        raise PhaseError(Refusal.INCOMPATIBLE_RESUME)
    return before.observed, served


def _history(journal: Journal) -> tuple[int, int, dict[int, int]]:
    """What the journal's observations already hold: the next request ordinal, the next trial
    execution, and each trial's latest execution. `PhaseError` for observations that cannot be read."""
    next_request = execution = 0
    executions: dict[int, int] = {}
    try:
        observed = [one for attempt in journal.attempts for one in journal.observations(attempt.ordinal)]
    except JournalError:
        raise PhaseError(Refusal.INCOMPATIBLE_RESUME) from None
    for one in observed:
        if isinstance(one, RequestRecord):
            next_request = max(next_request, one.request + 1)
        elif isinstance(one, TrialRecord):
            execution = max(execution, one.execution + 1)
            executions[one.trial] = max(executions.get(one.trial, one.execution), one.execution)
    return next_request, execution, executions


def _write_whole(directory: Path, name: str, text: str) -> None:
    """A file of the journal's directory, written whole: to a file beside it, then renamed."""
    beside = directory / (name + ".tmp")
    descriptor = os.open(beside, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(text + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(beside, directory / name)


def _write_manifest(directory: Path, manifest: Manifest) -> None:
    """The manifest, written whole beside the journal."""
    _write_whole(directory, MANIFEST, dumps(manifest))


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
    rehearsed = _with_cell(plan_, rehearsal)
    mock = replay.Gateway("https://driver-check.example.com", "driver-check", clock, httpx.MockTransport(_mock_answer))
    ran = await replay.run_cell(rehearsed, largest.identity.cell, mock, keys)
    lag, tolerance = ran.summary.lag_p95_ms, ran.summary.lag_tolerance_ms
    return lag is not None and lag <= tolerance


def _with_cell(plan_: Plan, cell: PlannedCell) -> Plan:
    """The plan with one of its cells driven otherwise: a rehearsal, or the widened warm-up."""
    at = cell.identity.cell
    return dataclasses.replace(plan_, cells=(*plan_.cells[:at], cell, *plan_.cells[at + 1 :]))


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


def _finite(value: float) -> bool:
    return math.isfinite(value) and value >= 0


def _cumulative(samples: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    """A counter series as the increase it counted since its first sample: each rise added, each
    drop a restart from zero whose new value is added in full, as telemetry counts it."""
    added, found = 0.0, []
    for index, (at_ms, value) in enumerate(samples):
        if index:
            before = samples[index - 1][1]
            added += value - before if value >= before else value
        found.append((at_ms, added))
    return found


def _summed(series: Sequence[Sequence[tuple[int, float]]]) -> list[tuple[int, float]]:
    """Cumulative series summed at each time any of them was scraped, each at its latest value."""
    found = []
    for at_ms in sorted({at for one in series for at, _ in one}):
        found.append((at_ms, sum(next((v for t, v in reversed(one) if t <= at_ms), 0.0) for one in series)))
    return found


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
    the window's start to one after its drain, on the run's clock. A gauge is its series at one
    scrape summed, or averaged for a ratio. A counter is the increase each of its series counted
    since its first sample of that range, summed: a lifetime total beyond the contracts' bound, or a
    series that restarted while another went on, counts what the window counted. A host is the
    reading's ordinal of its instance."""
    hosts = {host.host for host in reading.hosts}
    low, high = window.started - telemetry.ALLOWANCE_SECONDS, window.drained + telemetry.ALLOWANCE_SECONDS
    found: list[HostSample] = []
    for metric in HostMetric:
        counter = metric in telemetry.COUNTERS
        by: dict[int, list[list[tuple[int, float]]]] = {}
        for one in answers.get(metric, ()):
            instance = one.labels.get("instance", "")
            inside = any(window.started <= at <= window.drained for at, _ in one.samples)
            if not instance or not inside or (host := ordinals.of(instance)) not in hosts:
                continue
            held = sorted(
                (round((at - origin_s) * 1000), value)
                for at, value in one.samples
                if low <= at <= high and math.isfinite(at) and _finite(value) and (counter or value <= MAX_COUNT)
            )
            held = [(at_ms, value) for at_ms, value in held if 0 <= at_ms <= MAX_TIME_MS]
            by.setdefault(host, []).append(_cumulative(held) if counter else held)
        for host, series in sorted(by.items()):
            if counter:
                values = _summed(series)
            else:
                combine = telemetry.GAUGES.get(metric, sum)
                scrapes: dict[int, list[float]] = {}
                for one in series:
                    for at_ms, value in one:
                        scrapes.setdefault(at_ms, []).append(value)
                values = [(at_ms, combine(held)) for at_ms, held in sorted(scrapes.items())]
            for at_ms, value in values:
                if value > MAX_COUNT or (metric is HostMetric.GPU_UTILIZATION and value > 1):
                    continue
                found.append(
                    HostSample(cell=cell, host=host, at_ms=at_ms, metric=metric, unit=METRIC_UNITS[metric], value=value)
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


def reconciled(tally: Tally, rows: Sequence[RequestRecord]) -> bool:
    """Whether a scheduler's tally is its request rows: every send ended and has one row of its
    own, the completions and each cause of failure are as many as the rows say, and the streams it
    found eligible for a speed are as many as the rows give one (`capacity.speed_of`), as offline
    grading reads them."""
    outcomes = Counter(one.outcome for one in rows)
    causes = {cause: tally.failures.get(cause, 0) for cause in RequestOutcome if cause is not RequestOutcome.COMPLETED}
    return (
        tally.unfinished == 0
        and tally.attempts == len(rows) == len({one.request for one in rows})
        and tally.completed == outcomes[RequestOutcome.COMPLETED]
        and all(outcomes[cause] == count for cause, count in causes.items())
        and tally.eligible == sum(capacity.speed_of(one) is not None for one in rows)
    )


def _lag_p95(rows: Sequence[RequestRecord]) -> float | None:
    lags = [float(one.sent_ms - one.eligible_ms) for one in rows]
    return percentile(lags, 95) if lags else None


def _liveness_tokens(seconds: float) -> int:
    """At most what a host's own liveness completions count over `seconds`, as telemetry reads it."""
    return int((seconds + 2 * telemetry.ALLOWANCE_SECONDS) // PROBE_INTERVAL_SECONDS) + 1


def _agreed(observed: Iterable[Settings]) -> Settings:
    """What every observation observed alike, setting by setting; `None` where they differ or none did."""
    held = list(observed)
    agreed = {}
    for spec in dataclasses.fields(Settings):
        values = {getattr(one, spec.name) for one in held}
        agreed[spec.name] = values.pop() if len(values) == 1 else None
    return Settings(**agreed) if held else _nothing_observed()


type Serving = dict[str, object]


def serving(reading: telemetry.Reading) -> Serving:
    """What identifies what served a window, of what it observed (`SERVING`), and the one launch
    fingerprint its engines said: `None` for a part the hosts did not all show alike."""
    return {name: getattr(reading.observed, name) for name in SERVING} | {LAUNCH: reading.observed_launch_fingerprint}


def served_alike(reading: telemetry.Reading) -> set[InvalidFlag]:
    """Whether every host of the window served the same thing, declared or not: one instance type,
    image, model revision and launch fingerprint, the same on each host. A part a host does not
    show is `missing_evidence`; hosts, or a host, that showed two are `host_turnover`."""
    flags: set[InvalidFlag] = set()
    parts = [
        (host.instance_type, host.image_digest, host.model_revision, host.launch_fingerprints) for host in reading.hosts
    ]
    if any(None in part[:3] or not part[3] for part in parts):
        flags.add(InvalidFlag.MISSING_EVIDENCE)
    if len(set(parts)) > 1 or any(len(part[3]) > 1 for part in parts):
        flags.add(InvalidFlag.HOST_TURNOVER)
    return flags


def _load_served(directory: Path) -> Serving | None:
    try:
        held = json.loads((directory / SERVED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(held, dict) or set(held) != {*SERVING, LAUNCH} or None in held.values():
        return None
    return held


def _write_served(directory: Path, served: Serving) -> None:
    """What served the measurements a resume keeps, written whole beside the manifest."""
    _write_whole(directory, SERVED, json.dumps(served, sort_keys=True))


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


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def calls_the_tool(route: Route, data: Mapping[str, Any]) -> bool:
    """Whether one event of a route's stream calls the route warm-up's tool: a `tool_use` block on
    Messages, a `function_call` item on Responses, a tool call's function on Chat Completions."""
    if route is Route.MESSAGES:
        block = _dict(data.get("content_block"))
        return (
            data.get("type") == "content_block_start"
            and block.get("type") == "tool_use"
            and (block.get("name") == WARM_UP_TOOL)
        )
    if route is Route.RESPONSES:
        item = _dict(data.get("item"))
        return item.get("type") == "function_call" and item.get("name") == WARM_UP_TOOL
    choices = data.get("choices")
    for choice in choices if isinstance(choices, list) else ():
        calls = _dict(_dict(choice).get("delta")).get("tool_calls")
        for call in calls if isinstance(calls, list) else ():
            if _dict(_dict(call).get("function")).get("name") == WARM_UP_TOOL:
                return True
    return False


class _ToolCall:
    """Reads a route's stream beside the driver's reader, for a call of the warm-up's tool."""

    def __init__(self, route: Route) -> None:
        self.route, self.events, self.called = route, SseReader(), False

    def feed(self, chunk: bytes) -> None:
        for event in self.events.feed(chunk):
            if event.comment or self.called:
                continue
            try:
                data = json.loads(event.data)
            except ValueError:
                continue
            self.called = isinstance(data, dict) and calls_the_tool(self.route, data)


# --- The run -------------------------------------------------------------------------------------


class _Recorded:
    """The harnesses, keeping what each attempt's forwarder observed, by its trial. An attempt
    that raises before it is graded (its worker's cleanup failed, or it was cancelled) is handed
    to `lost` with what it observed, which would otherwise be lost with it."""

    def __init__(self, inner: trials.Harnesses) -> None:
        self.inner = inner
        self.evidence: dict[int, Evidence] = {}
        self.lost: Callable[[int, Evidence], None] = lambda _trial, _evidence: None

    @contextlib.asynccontextmanager
    async def attempt(self, trial: TrialIdentity, key: str, cancel: asyncio.Event) -> AsyncGenerator[trials.Work]:
        try:
            async with self.inner.attempt(trial, key, cancel) as work:
                self.evidence[trial.trial] = work.evidence
                yield work
        except BaseException:
            if (evidence := self.evidence.pop(trial.trial, None)) is not None:
                self.lost(trial.trial, evidence)
            raise


class _Sink:
    """One cell's attempt in the journal: each observation is appended the moment it is made."""

    def __init__(self, runner: _Run, attempt: int) -> None:
        self.runner, self.attempt = runner, attempt
        self.requests: list[RequestRecord] = []
        self.trials: list[TrialRecord] = []
        self.samples: list[HostSample] = []

    def request(self, record: RequestRecord) -> None:
        self.runner.journal.append(self.attempt, record)
        self.requests.append(record)
        self.runner.next_request = max(self.runner.next_request, record.request + 1)

    def trial(self, record: TrialRecord) -> None:
        self.runner.journal.append(self.attempt, record)
        self.trials.append(record)

    def sample(self, sample: HostSample) -> None:
        self.runner.journal.append(self.attempt, sample)
        self.samples.append(sample)


@dataclass
class _Driven:
    """What driving a cell left, before its telemetry is read."""

    record: CellRecord | None
    identities: int
    tallies: list[Tally] = field(default_factory=list)
    live: list[tuple[int, Evidence]] = field(default_factory=list)
    flags: set[InvalidFlag] = field(default_factory=set)
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
        prior: tuple[Settings, Serving] | None,
        keep: Keep | None = None,
    ) -> None:
        self.plan, self.admitted, self.gateway, self.keys, self.aliases = plan_, admitted, gateway, keys, aliases
        self.source, self.harnesses, self.graders, self.journal = source, _Recorded(harnesses), graders, journal
        self.manifest, self.cancel, self.origin_s, self.deadline_ms = manifest, cancel, origin_s, deadline_ms
        self.keep = keep
        self.clock = gateway.clock
        self.tolerance = plan_.spec.protocol["driver"]["scheduling_lag_p95_ms_max"]
        self.ordinals = telemetry.Ordinals()
        self.evidence = replay.Evidence()
        self.warm_up: telemetry.Window | None = None
        # What served the measurements a resume keeps (`SERVED`); what served this run's warm-up;
        # and what every qualifying window observed alike, the kept ones' included.
        self.prior = None if prior is None else prior[1]
        self.served: Serving | None = None
        self.observed: Settings | None = None if prior is None else prior[0]
        self.ended: Ended | None = None
        self.breach: booking.Reason | None = None
        self.abandoned: set[asyncio.Future[booking.Reason | None]] = set()
        session = admitted.outcome.session
        self.hosts = 0 if session is None else session.instances
        # Ordinals go on from what the journal holds: a resume adds to the run, it does not restart it.
        self.next_request, self.execution, self.executions = _history(journal)

    # Times.

    def epoch(self, at_ms: int) -> float:
        return self.origin_s + at_ms / 1000

    def window(self, started_ms: int, ended_ms: int) -> telemetry.Window:
        return telemetry.Window(self.epoch(started_ms), self.epoch(max(ended_ms, started_ms + 1)))

    # Stopping.

    def _now(self) -> Ended | None:
        """The cancellation and the deadline, as read at this instant."""
        if self.cancel.is_set():
            return Ended.CANCELLED
        if self.clock.now_ms() >= self.deadline_ms:
            return Ended.DEADLINE
        return None

    async def _continuity(self) -> booking.Reason | None:
        try:
            return await self.admitted.continuity()
        except Exception:  # noqa: BLE001 - a probe that cannot answer establishes no continuity
            return booking.Reason.POOL_STATE_UNREADABLE

    async def _tripped(self) -> Ended | None:
        """Whether the run must stop: cancelled, past its deadline, or out of its booking. The
        cancellation and the deadline are read again while the booking's probe is pending, which
        they cut short, and once more after it has answered."""
        if (now := self._now()) is not None:
            return now
        probe = asyncio.ensure_future(self._continuity())
        try:
            await _first(asyncio.shield(probe), self.cancel.wait(), _until(self.clock, self.deadline_ms))
        finally:
            if not probe.done():
                # Cut short: the probe's own cleanup goes on without holding the stop back.
                probe.cancel()
                self.abandoned.add(probe)
        if (now := self._now()) is not None:
            return now
        if probe.cancelled():
            self.breach = self.breach or booking.Reason.POOL_STATE_UNREADABLE
            return Ended.CONTINUITY_LOST
        breach = probe.result()
        if breach is not None:
            self.breach = self.breach or breach
            return Ended.CONTINUITY_LOST
        return None

    def _halted(self, stop: asyncio.Event) -> bool:
        """Whether the cell is stopped, as read at this instant: the watcher may not have run since
        the cancel event was set or the deadline passed."""
        if not stop.is_set():
            tripped = self._now()
            if tripped is not None:
                self.ended = self.ended or tripped
                stop.set()
        return stop.is_set()

    async def _watch(self, stop: asyncio.Event) -> None:
        """Sets `stop` once the run is cancelled, past its deadline, or out of its booking. It
        never ends quietly: whatever ends it, the cell is stopped."""
        try:
            while not stop.is_set():
                tripped = await self._tripped()
                if tripped is not None:
                    self.ended = self.ended or tripped
                    return
                wake = min(self.clock.now_ms() + WATCH_MS, self.deadline_ms)
                await _first(_until(self.clock, wake), self.cancel.wait(), stop.wait())
        except Exception:  # noqa: BLE001 - a watcher that broke can no longer show the booking held
            self.breach = self.breach or booking.Reason.POOL_STATE_UNREADABLE
            self.ended = self.ended or Ended.CONTINUITY_LOST
        finally:
            stop.set()

    async def _bounded[T](self, work: Awaitable[T], stop: asyncio.Event, drain_ms: int) -> T | None:
        """Runs a cell's work until it ends or the cell is stopped. Once it is stopped, the work has
        `drain_ms` to end and is then cancelled: `None` for work that had to be."""
        task = asyncio.ensure_future(work)
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
            await asyncio.gather(task, return_exceptions=True)

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
        try:
            return await self._cells(cells)
        finally:
            # The probes a stop cut short finish their cleanup once traffic has stopped.
            await asyncio.gather(*self.abandoned, return_exceptions=True)

    async def _cells(self, cells: Sequence[int]) -> list[CellResult]:
        done: list[CellResult] = []
        for cell in cells:
            if self.ended is not None:
                break
            planned = self.plan.cells[cell]
            warm = planned.identity.phase is Phase.WARM_UP
            if not warm and any(one.cell == cell and one.reusable for one in self.journal.attempts):
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
        warm = planned.identity.phase is Phase.WARM_UP
        attempt = self.journal.start_cell(
            planned.identity.cell, self.plan.workload_hash, self.manifest.treatment_id, again=warm
        ).ordinal
        sink = _Sink(self, attempt)
        stop = asyncio.Event()
        watch = asyncio.ensure_future(self._watch(stop))
        try:
            return await self._measured(planned, sink, stop)
        except BaseException:
            # A driver that raised: what the cell observed stays, in an attempt that cannot count.
            if self.journal.attempts[attempt].status is AttemptStatus.RUNNING:
                with contextlib.suppress(JournalError):
                    self.journal.interrupt(attempt)
            raise
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

    async def _measured(self, planned: PlannedCell, sink: _Sink, stop: asyncio.Event) -> CellResult:
        number = planned.identity.cell
        baseline = self._baseline()
        started_ms = self.clock.now_ms()
        if planned.identity.phase is Phase.WARM_UP:
            driven = await self._warm_up_cell(planned, sink, stop)
        elif planned.identity.phase in LIVE_PHASES:
            driven = await self._live_cell(planned, sink, stop)
        else:
            driven = await self._replay_cell(planned, sink, stop)
        tally = _tally(driven.tallies) if driven.tallies else None
        record, reading, window = driven.record, None, None
        if record is not None and self.ended is None and not self._halted(stop):
            started_ms = min(started_ms, record.started_ms)
            window = self.window(started_ms, record.ended_ms)
            # The scrapes of the window are awaited under the watcher: a stop meanwhile stops the cell.
            readable_ms = math.ceil((telemetry.readable_from(window) - self.origin_s) * 1000)
            await _first(_until(self.clock, readable_ms), stop.wait())
            if not self._halted(stop):
                source = self.source
                warm_up = None if planned.identity.phase is Phase.WARM_UP else self.warm_up
                attempts = len(sink.requests) - driven.side_queries
                driver = self._driver(attempts, driven.side_queries, warm_up, baseline)
                answers = telemetry.read(source.reader, source.pool, source.engine, window, warm_up)
                reading = telemetry.resolve(answers, window, source.requested, driver, self.ordinals)
                for sample in host_samples(
                    answers, reading, window, self.ordinals, cell=number, origin_s=self.origin_s
                ):
                    sink.sample(sample)
                record = self._judged(planned, record, driven, sink, answers, window, reading, baseline, started_ms)
        warm = planned.identity.phase is Phase.WARM_UP
        # A resume keeps measurements only of what served them: this booking must serve it too.
        prior = self.prior
        if reading is not None and warm and prior is not None and serving(reading) != prior:
            self.ended = self.ended or Ended.SERVING_CHANGED
        if reading is not None and self.ended is None and not stop.is_set():
            # The booking must still hold once the window has been read: it held through all of it.
            tripped = await self._tripped()
            if tripped is not None:
                self.ended = self.ended or tripped
        checkpointed = record is not None and reading is not None and self.ended is None and not self._halted(stop)
        if checkpointed and record is not None and reading is not None:
            if self.keep is not None:
                # What the bundle needs beside the journal, saved before the checkpoint that
                # makes the cell one a resume keeps.
                self.keep(sink.attempt, tally, reading)
            self.journal.checkpoint_cell(sink.attempt, record)
            if warm:
                self.warm_up = window
                self.served = serving(reading)
            if record.valid_for_qualification:
                # Only what can be kept says what served the run: an invalid window's observations
                # stay in its attempt, for diagnosis, and a resume runs it again.
                if not warm:
                    _write_served(self.journal.directory, serving(reading))
                self.observed = _agreed([*([] if self.observed is None else [self.observed]), reading.observed])
                _write_manifest(self.journal.directory, dataclasses.replace(self.manifest, observed=self.observed))
        else:
            self.journal.interrupt(sink.attempt)
            if record is not None:
                # What an interrupted cell recorded is kept, as a window that did not end whole.
                record = dataclasses.replace(record, status=CellStatus.INCOMPLETE)
        return CellResult(
            cell=planned,
            attempt=sink.attempt,
            record=record,
            requests=tuple(sorted(sink.requests, key=lambda one: one.request)),
            trials=tuple(sink.trials),
            samples=tuple(sink.samples),
            tally=tally,
            reading=reading,
            checkpointed=checkpointed,
        )

    def _judged(
        self,
        planned: PlannedCell,
        record: CellRecord,
        driven: _Driven,
        sink: _Sink,
        answers: Mapping[telemetry.Key, Sequence[Series]],
        window: telemetry.Window,
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
        if baseline.missing or baseline.hosts != self.hosts:
            flags.add(InvalidFlag.MISSING_EVIDENCE)
        # What served the window is what was booked: the hosts selected, one release on them.
        if len(reading.hosts) != self.hosts:
            flags.add(InvalidFlag.HOST_TURNOVER)
        for treatment in BOOKED:
            reason = reading.treatments.get(treatment)
            if reason is not None and reason is not telemetry.Reason.NOT_DECLARED:
                flags.add(InvalidFlag.MISSING_EVIDENCE if reason in _UNSEEN else InvalidFlag.HOST_TURNOVER)
        # What served the window is what served the warm-up, declared or not: a part it showed then
        # and does not show now is unestablished, and one it shows otherwise changed.
        flags |= served_alike(reading)
        if self.served is not None and planned.identity.phase is not Phase.WARM_UP:
            now = serving(reading)
            for name, before in self.served.items():
                if before is not None and now[name] != before:
                    flags.add(InvalidFlag.MISSING_EVIDENCE if now[name] is None else InvalidFlag.HOST_TURNOVER)
        # Every seat sent as its own student, and the gateway counted each of them as the driver did.
        sent = Counter(one.student for one in sink.requests)
        if len(sent) < driven.identities:
            flags.add(InvalidFlag.MISSING_EVIDENCE)
        counted = telemetry.identity_requests(answers, window, self.aliases)
        for student, alias in enumerate(self.aliases):
            seen = counted[alias]
            if (0 if seen is None else round(seen)) != sent[student]:
                flags.add(InvalidFlag.UNRECONCILED_COUNTERS)
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
        if not reconciled(ran.summary.tally, ran.requests):
            flags.add(InvalidFlag.UNRECONCILED_COUNTERS)
        return flags

    async def _replay_cell(self, planned: PlannedCell, sink: _Sink, stop: asyncio.Event) -> _Driven:
        work = replay.run_cell(
            self.plan,
            planned.identity.cell,
            self.gateway,
            self.keys,
            stops=replay.Stops(cancel=stop, deadline_ms=self.deadline_ms),
            first_request=self.next_request,
            evidence=self.evidence,
            on_request=sink.request,
        )
        ran = await self._bounded(work, stop, planned.drain_ms)
        if ran is None:
            return _Driven(None, planned.identities)
        return _Driven(ran.record, planned.identities, [ran.summary.tally], flags=self._replay_flags(ran))

    async def _route_warm_up(self, planned: PlannedCell, sink: _Sink, stop: asyncio.Event, tally: Attempts) -> bool:
        """One streamed tool exchange on each route of the suite, as the cohort's first student,
        under the warm-up's prefix: whether each answer completed with a call of the tool. None is
        sent once the cell is stopped."""
        gateway, clock = self.gateway, self.clock
        prefix = f"warm-up {planned.input_seed} namespace {planned.prefix_namespace}"
        exchanged = True
        async with httpx.AsyncClient(
            base_url=gateway.endpoint, timeout=loadgen.TIMEOUT, transport=gateway.transport, trust_env=False
        ) as http:
            for conversation, name in enumerate(self.plan.spec.protocol["warm_up"]["routes"]):
                if self._halted(stop):
                    return False
                route = Route(name)
                path, headers, body = route_exchange(route, gateway.model, f"{prefix} route {route.value}")
                headers |= {"authorization": f"Bearer {self.keys[0]}"}
                eligible = sent = clock.now_ms()
                reader, called = tally.send(route, sent), _ToolCall(route)
                ordinal, self.next_request = self.next_request, self.next_request + 1
                try:
                    async with http.stream("POST", path, headers=headers, content=body) as answer:
                        reader.answered(answer.status_code)
                        async for chunk in answer.aiter_bytes():
                            reader.feed(chunk, clock.now_ms())
                            called.feed(chunk)
                except httpx.HTTPError:
                    reader.end(clock.now_ms(), transport_error=True)
                finally:
                    # The exchange as it ended, or, cut, as far as it had come.
                    ended = reader.end(clock.now_ms())
                    sink.request(
                        RequestRecord(
                            request=ordinal,
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
                exchanged = exchanged and not ended.failed and called.called
        return exchanged

    def _widened(self, planned: PlannedCell) -> PlannedCell:
        """The warm-up's replay on one seat for each canary of the cohort, in the warm-up's shape,
        each from the start: whichever host a student's affinity sends to, a student works it."""
        slots = len(self.keys) * self.plan.spec.conversations[planned.identity.shape]
        return dataclasses.replace(planned, identities=len(self.keys), replay_slots=slots, arrivals_ms=(0,) * slots)

    async def _warm_up_cell(self, planned: PlannedCell, sink: _Sink, stop: asyncio.Event) -> _Driven:
        started_ms = self.clock.now_ms()
        attempts = Attempts()
        routed = await self._bounded(self._route_warm_up(planned, sink, stop, attempts), stop, planned.drain_ms)
        routes = list(sink.requests)
        if not routed:
            if not self._halted(stop):
                self.ended = self.ended or Ended.ROUTE_WARM_UP_FAILED
            return _Driven(None, 1, [attempts.tally()])
        flags = set() if reconciled(attempts.tally(), routes) else {InvalidFlag.UNRECONCILED_COUNTERS}
        widened = self._widened(planned)
        finished = asyncio.Event()
        until_ms = min(self.deadline_ms, started_ms + self.plan.spec.estimates.warm_up_ms)
        work = replay.run_cell(
            _with_cell(self.plan, widened),
            planned.identity.cell,
            self.gateway,
            self.keys,
            stops=replay.Stops(finished=finished, cancel=stop, deadline_ms=until_ms),
            first_request=self.next_request,
            evidence=self.evidence,
            first_conversation=len(routes),
            on_request=sink.request,
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
            return _Driven(None, widened.identities, [attempts.tally()])
        if not finished.is_set() and self.ended is None:
            self.ended = Ended.WARM_UP_INCOMPLETE
        record = dataclasses.replace(ran.record, started_ms=started_ms)
        flags |= self._replay_flags(ran)
        return _Driven(record, widened.identities, [attempts.tally(), ran.summary.tally], flags=flags)

    def _live_attempt(self, planned: PlannedCell, sink: _Sink, driven: _Driven) -> Callable[[trials.Executed], int]:
        """What is kept of each live attempt once it is graded: its trial record, linked to the
        execution it replaces, and its forwarder's requests, numbered from the next ordinal."""

        def kept(executed: trials.Executed) -> int:
            record = executed.record
            earlier = self.executions.get(record.trial)
            if earlier is not None and earlier < record.execution:
                record = dataclasses.replace(record, replaces=earlier)
            self.executions[record.trial] = record.execution
            self.execution = max(self.execution, record.execution + 1)
            evidence = self.harnesses.evidence.pop(record.trial, None)
            taken = 0 if evidence is None else self._live_requests(planned, sink, driven, record.trial, evidence)
            sink.trial(record)
            return taken

        return kept

    def _live_requests(self, planned: PlannedCell, sink: _Sink, driven: _Driven, trial: int, evidence: Evidence) -> int:
        """Records what a live attempt's forwarder measured, numbered from the next ordinal, and
        checks it: its tally against its rows, and its starting cache from reported usage (the
        first answers met the prefix uncached). Gives how many ordinals it took."""
        rows = []
        for turn, measured in enumerate(evidence.requests):
            rows.append(
                measured.request_record(
                    request=self.next_request,
                    cell=planned.identity.cell,
                    student=trials.LIVE_STUDENT,
                    conversation=len(driven.live),
                    turn=turn,
                    trial=trial,
                )
            )
            sink.request(rows[-1])
        driven.live.append((trial, evidence))
        driven.tallies.append(evidence.tally)
        driven.side_queries += sum(one.side_call for one in evidence.requests)
        if not reconciled(evidence.tally, rows):
            driven.flags.add(InvalidFlag.UNRECONCILED_COUNTERS)
        # The task's starting cache is read from the requests under its prefix: a side query has
        # its own, and is counted in the accounting only.
        primary = [row for row, measured in zip(rows, evidence.requests, strict=True) if not measured.side_call]
        if replay.met_fresh(primary) is not True:
            driven.flags.add(InvalidFlag.MISSING_EVIDENCE)
        return len(rows)

    def _lost(self, planned: PlannedCell, sink: _Sink, driven: _Driven, trial: int, evidence: Evidence) -> None:
        with contextlib.suppress(JournalError, ValueError):
            self._live_requests(planned, sink, driven, trial, evidence)

    async def _live_cell(self, planned: PlannedCell, sink: _Sink, stop: asyncio.Event) -> _Driven:
        self.harnesses.evidence.clear()
        driven = _Driven(None, planned.identities)
        # An attempt that raised before it was graded leaves what it measured in the cell's attempt.
        self.harnesses.lost = lambda trial, evidence: self._lost(planned, sink, driven, trial, evidence)
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
            on_request=sink.request,
            on_attempt=self._live_attempt(planned, sink, driven),
        )
        ran = await self._bounded(work, stop, planned.drain_ms + round(CANCEL_S * 1000))
        if ran is None or not ran.cells:
            return driven
        (cell,) = ran.cells
        background = cell.background
        for one in background.runs:
            driven.flags |= self._replay_flags(one)
        driven.tallies[:0] = [one.summary.tally for one in background.runs]
        lag = _lag_p95([one for one in sink.requests if one.traffic is Traffic.LIVE])
        if lag is not None and lag > self.tolerance:
            driven.flags.add(InvalidFlag.DRIVER_SATURATION)
        record = background.record
        counted = {one.record.trial for one in cell.trials if one.record.counts_as_attempt}
        if record is not None and counted != set(planned.trials):
            record = dataclasses.replace(record, status=CellStatus.INCOMPLETE)
        driven.record = record
        return driven


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
    keep: Keep | None = None,
) -> Result:
    """Runs the plan's cells in order in the admitted booking (the module's text): all of them
    unless `cells` names some, the warm-up first in any case. `origin_s` is the time, in seconds
    since the epoch, at which the run's clock (`gateway.clock`) read zero. `PhaseError`, with
    nothing sent to the gateway, for a run that cannot be measured. `keep`, when given, is told
    each attempt that is about to be checkpointed, with its scheduler's tally and its reading."""
    session = admitted.outcome.session
    if not admitted.outcome.ready or session is None:
        raise PhaseError(Refusal.BOOKING_NOT_READY)
    if gateway.model != session.pool or source.pool != session.pool:
        raise PhaseError(Refusal.BOOKING_MISMATCH)
    keys, aliases = cohort(plan_, canaries)
    chosen = [one.identity.cell for one in plan_.cells] if cells is None else [0, *(one for one in cells if one)]
    _graders_held(plan_, chosen, graders)
    manifest = resolve_manifest(plan_, artifacts, source.requested.settings)
    prior = _resumable(plan_, journal, manifest)
    if prior is not None:
        manifest = dataclasses.replace(manifest, observed=prior[0])
    _history(journal)
    clock = gateway.clock
    found = telemetry.preflight(source.reader, source.pool, source.engine, origin_s + clock.now_ms() / 1000)
    if found.missing or found.hosts < session.instances:
        raise PhaseError(Refusal.TELEMETRY_UNAVAILABLE)
    if found.hosts > session.instances:
        raise PhaseError(Refusal.BOOKING_MISMATCH)
    if not await headroom(plan_, clock, keys, admissions_ms=headroom_ms):
        raise PhaseError(Refusal.DRIVER_HEADROOM_MISSED)
    _write_manifest(journal.directory, manifest)
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
        prior,
        keep,
    )
    done = await runner.run(chosen)
    # The manifest was written again at each checkpoint: what every window it keeps observed alike.
    if runner.observed is not None:
        manifest = dataclasses.replace(manifest, observed=runner.observed)
    return Result(manifest=manifest, cells=tuple(done), ended=runner.ended or Ended.COMPLETED, breach=runner.breach)


__all__ = [
    "Admitted",
    "Artifacts",
    "Canaries",
    "CellResult",
    "Ended",
    "Keep",
    "PhaseError",
    "Refusal",
    "Result",
    "Telemetry",
    "run",
]
