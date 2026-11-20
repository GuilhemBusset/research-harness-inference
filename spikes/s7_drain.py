"""Spike S7: what scale-in and a gateway deployment do to a request, on the mock pool (system design,
sections 5.1, 8.5 and 12).

AWS's documentation does not settle whether a group waits the whole deregistration delay for a
target that has no connections, nor whether it drains at all before stopping an instance into the
warm pool (evidence E13). The design does not lean on a long delay (section 5.1), and gives the
gateway's target group 1800 seconds so that a held request or a long stream outlives a gateway
deployment (section 8.5). This job asks both of the mock pool, for cents, before any GPU depends on
them. **Pass:** an idle instance stops within a minute or two, which the verdict reads as
`LEAVE_PASS_SECONDS`, 120 seconds, on each path; in-flight requests finish; no wake is lost.

**What it does**, through the gateway only, as canary students (`common.canary_keys`), and reading
the pool only from the workspace and the pool-state table. It never changes the pool: the scaler
alone moves it, by load and by idleness (section 5.1).

1. **The stream across a gateway deployment** (`Run.across_deployment`). It notes the gateway's
   tasks, opens a held stream and prints when the hold begins and how long it may last. The owner
   starts the deployment, by releasing the pipeline again: the job starts none. The stream is held
   until a gateway task the job did not see at its start has been writing for
   `ROLLOUT_MARGIN_SECONDS`, by which ECS has replaced the tasks it began with, or for
   `RHI_S7_DEPLOYMENT_HOLD_SECONDS` at most.
2. **Two instances.** It holds twice the pool's `target_concurrency` of short streams until the
   scaler asks for two instances and reads two in service, then stops the load.
3. **The departures.** It sends nothing more but one held stream, as one canary student, opened
   while the scaler still asks for two and held until the first instance the scaler takes away
   has stopped: the stream across a scale-in. It watches the scaler's note: each time the note
   asks for fewer instances, the scaler has chosen an idle one to leave, and the note's
   `written_at` is when the leave began.
4. **The wakes** (`Run.send_wakes`). As many times as `RHI_S7_WAKES` gives, it makes sure the pool
   is awake, waits until the scaler asks for no instance and reads none in service, which it does
   only after the pool's idle period, and sends one request at once. A wake is lost when its
   request does not complete (below) within the pool's hold and `WAKE_MARGIN_SECONDS`. After a lost
   wake, it sends requests to bring the pool back, up to `RECOVERIES` of them, which are not wakes
   and are not counted, and goes on to the next.

**A held stream** (`slow_stream`). The mock streams its canned turn in well under a second, and
the gateway reads a request's whole body before it does anything with it, so a stream is held on
the server by its request: the job sends the body but its last byte, then one byte of padding
every `TRICKLE_SECONDS`, white space that JSON allows before the closing brace, and the last byte
once the event the stream spans is over. Until then no server can have answered it: it is open on
the gateway task that took it. A stream completed when it was answered 200 and its closing event,
`message_stop`, arrived, with no error event.

**When the gateway was deployed.** The gateway's collector writes the series `up` of its scrape,
job `gateway`, labelled with the task's ID, every `SCRAPE_SECONDS` while the task runs. A task that
first writes after the stream opened is one the deployment started (`rollout_at`). The stream's
number is measured (`across_rollout`): 1 when it completed and was held until such a task had
written for `ROLLOUT_MARGIN_SECONDS`; 0 when it did not complete and such a task wrote while it was
open. Otherwise the job saw no deployment under the stream, and the run is not measured.

**When an instance has left.** The scaler's gauges cannot say it: an instance stops counting as in
service as soon as its target is draining, and the warm count holds an instance from the moment it
enters the warm pool's wait state, before it is stopped (`rhi_scaler.aws_io`). An instance has
stopped, terminated or stopped into the warm pool, when its node's collector stops writing. The
collector's Prometheus receiver writes the series `up` for each of its scrape targets every
`SCRAPE_SECONDS`, the DCGM target's included even where nothing listens there, labelled with the
instance's ID and its pool; it runs until the instance shuts down, after the engine's unit, which
the lifecycle agent stops only once the termination hook is complete (`rhi_lifecycle.termination`).
So the instance that left is the one, among those writing when the note asked for fewer, whose
last write is `QUIET_SECONDS` old, and it took `leave_seconds` to leave: from the note's
`written_at` to its last write, plus one scrape, by which it had stopped. When none has stopped
`LEAVE_WAIT_SECONDS` after the note, no stop was seen: that departure has no path, and is not a
number.

**Which path it took** (`path_of`), from the scaler's readings of the warm count, each at the time
the scaler wrote it (`Pool.warm_counts`), which counts an instance going back to the warm pool from
its first wait state. The path is told only from a reading at or before the note and one at or
after the instance stopped (`warm_around`): with them, the warm count rose and no instance new to
the pool wrote, so the instance went back to the warm pool; the warm count did not rise, so it was
terminated. Without them, or with a rise while a new instance wrote (a fresh instance launched into
the warm pool in place of a terminated one), that departure has no path, and is not a number. A
run whose departures did not take both paths takes the pool to two instances again, up to `CYCLES`
times, and is not measured when a path is still missing. A path seen twice keeps its longer time.

**The numbers** (`Number`): the seconds to leave service on each path, whether each held stream
completed (1 or 0), and the wakes sent and lost.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s7_drain`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |
| `RHI_S7_WAKES` | The wakes it sends, each at the moment the pool reaches zero; `DEFAULT_WAKES` unless given |
| `RHI_S7_DEPLOYMENT_HOLD_SECONDS` | The longest the stream across the gateway deployment is held; `DEFAULT_DEPLOYMENT_HOLD_SECONDS` unless given |
| `RHI_SPIKE_POOL` | The pool, `mock` unless given |

The pool's `target_concurrency`, `hold_seconds` and `idle_minutes` come from the deployed
configuration (`rhi.config.read_ssm`), and the region from CodeBuild's `AWS_REGION`. Run it while
nothing else sends to the pool: other load keeps instances from going idle.
"""

from __future__ import annotations

import asyncio
import math
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import boto3
import httpx

import common
import loadgen
from rhi import config
from rhi_admin.roster import DEFAULT_CONCURRENCY_LIMIT
from rhi_common.records import NOTE_RECORD, ScalerNote
from rhi_gateway.sse import SseReader
from rhi_scaler.metrics import INSTANCES_IN_SERVICE, WARM_INSTANCES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Mapping, Sequence

JOB = "s7_drain"
MEASUREMENT = "S7"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
POOL_STATE_TABLE_VARIABLE = "RHI_SPIKE_POOL_STATE_TABLE"
WAKES_VARIABLE = "RHI_S7_WAKES"
DEPLOYMENT_HOLD_VARIABLE = "RHI_S7_DEPLOYMENT_HOLD_SECONDS"
POOL_VARIABLE = "RHI_SPIKE_POOL"
DEFAULT_POOL = "mock"
DEFAULT_WAKES = 3
# Under the gateway target group's 1800 seconds of deregistration delay (section 8.5), and long
# enough for a release of the pipeline to reach the gateway once the owner has started it.
DEFAULT_DEPLOYMENT_HOLD_SECONDS = 1500

# Seconds: "an idle instance stops within a minute or two" (section 12), read as two minutes.
LEAVE_PASS_SECONDS = 120
# Seconds. The collectors scrape, and write `up`, every 15 seconds; an instance whose last write is
# this old has stopped.
SCRAPE_SECONDS = 15
QUIET_SECONDS = 90
# Seconds the job waits for an instance to stop after the scaler asked for fewer.
LEAVE_WAIT_SECONDS = 15 * 60
# Seconds. The scaler runs once a minute; its note is polled more often, so a decision is seen soon.
NOTE_POLL_SECONDS = 5
POLL_SECONDS = 15
# Seconds a gauge's last sample may be old and still be the scaler's last reading: one run's period,
# its 50-second timeout and a margin (as in `spikes/s8_stickiness.py`).
FRESH_SECONDS = 120
# Seconds. The scaler adds an instance after 60 seconds above target, and the mock starts in about
# a minute; it removes one after 10 minutes of load that one fits, and 15 since the scale-out, and
# the last after the pool's idle period (section 5.1).
SCALE_OUT_WAIT_SECONDS = 20 * 60
SCALE_IN_WAIT_SECONDS = 30 * 60
# The instances the job brings the pool to (`max_instances` of the mock pool), and how many times
# it does, to see both paths.
INSTANCES = 2
CYCLES = 2
# Seconds between two bytes of a held stream's body: the edge's idle timeout is far longer.
TRICKLE_SECONDS = 5
PADDING = b" "
# The longest the stream across a scale-in is held: the first departure's longest watch.
SCALE_IN_HOLD_SECONDS = SCALE_IN_WAIT_SECONDS + LEAVE_WAIT_SECONDS + 3 * QUIET_SECONDS
# Seconds a gateway task new to the job writes before the deployment counts as under the stream: ECS
# deregisters the tasks it replaces once their successors are healthy, a minute or two after these
# start (section 8.5).
ROLLOUT_MARGIN_SECONDS = 300
GATEWAY_JOB = "gateway"
# Seconds a wake may take beyond the pool's hold before it counts as lost.
WAKE_MARGIN_SECONDS = 120
# The requests the job sends, one after another, to bring the pool back after a lost wake.
RECOVERIES = 3
# The streams one student of the load holds: one under a student's limit (as in S8).
STREAMS_PER_STUDENT = DEFAULT_CONCURRENCY_LIMIT - 1
START_TOKENS = 200
CLOSING_EVENT = "message_stop"
ERROR_EVENT = "error"


class Number(StrEnum):
    """The names of the numbers the job posts."""

    LEAVE_SECONDS_TERMINATED = "leave_seconds_terminated"
    LEAVE_SECONDS_WARM_POOL = "leave_seconds_warm_pool"
    STREAM_COMPLETED_SCALE_IN = "stream_completed_scale_in"
    STREAM_COMPLETED_GATEWAY_DEPLOYMENT = "stream_completed_gateway_deployment"
    WAKES_SENT = "wakes_sent"
    WAKES_LOST = "wakes_lost"


class Path(StrEnum):
    """The two ways an instance leaves service."""

    TERMINATED = "terminated"
    WARM_POOL = "warm_pool"


LEAVE_NUMBER = {Path.TERMINATED: Number.LEAVE_SECONDS_TERMINATED, Path.WARM_POOL: Number.LEAVE_SECONDS_WARM_POOL}


# The verdict.


def passes(numbers: common.Numbers) -> bool:
    """An idle instance gone within `LEAVE_PASS_SECONDS` on each path, both held streams complete,
    and at least one wake sent with none lost."""
    return (
        all(numbers[name] <= LEAVE_PASS_SECONDS for name in LEAVE_NUMBER.values())
        and numbers[Number.STREAM_COMPLETED_SCALE_IN] == 1
        and numbers[Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT] == 1
        and numbers[Number.WAKES_SENT] >= 1
        and numbers[Number.WAKES_LOST] == 0
    )


SPIKE = common.Spike(MEASUREMENT, Number, passes)


# A departure.


def last_writes(series: Sequence[common.Series]) -> dict[str, float]:
    """The time each instance last wrote, from a range query of the time of its samples
    (`timestamp(...)`): the latest value of each instance's series."""
    return {instance: last for instance, (_, last) in spans(series).items()}


def spans(series: Sequence[common.Series]) -> dict[str, tuple[float, float]]:
    """The first and the last time each instance wrote, from a range query of the time of its
    samples (`timestamp(...)`)."""
    found: dict[str, tuple[float, float]] = {}
    for one in series:
        instance = one.labels.get("instance")
        if instance and one.samples:
            first, last = found.get(instance, (math.inf, 0.0))
            found[instance] = (min(first, *one.values()), max(last, *one.values()))
    return found


def stopped(written: Mapping[str, float], among: set[str], now: float) -> dict[str, float]:
    """The instances of `among` that have stopped by `now`, each with its last write: none in
    `QUIET_SECONDS`, or none at all in `written`, which then gives no time."""
    return {
        instance: written[instance]
        for instance in among
        if instance in written and written[instance] <= now - QUIET_SECONDS
    }


def leave_seconds(asked_at: float, last_written: float) -> float:
    """The seconds from the scaler asking for fewer to the instance having stopped: its last write
    and one scrape, by which the next write would have come."""
    return max(0.0, last_written + SCRAPE_SECONDS - asked_at)


def path_of(warm_before: int, warm_after: int, newcomer: bool) -> Path | None:
    """The path a departure took, from the warm count before the note and the highest after it,
    and whether an instance new to the pool wrote since: `None` when they do not tell."""
    if warm_after <= warm_before:
        return Path.TERMINATED
    return None if newcomer else Path.WARM_POOL


def warm_around(
    samples: Sequence[tuple[float, float]], at: float, through: float | None = None
) -> tuple[int, int] | None:
    """The warm count at `at`, the last reading at or before it, and the highest after it. `None`
    when no reading is at or before `at`, or, `through` given, none is at or after `through`: the
    count was not observed across the departure."""
    before = [value for moment, value in samples if moment <= at]
    if not before or (through is not None and not any(moment >= through for moment, _ in samples)):
        return None
    first = round(before[-1])
    return first, max([first, *(round(value) for moment, value in samples if moment > at)])


@dataclass(frozen=True)
class Departure:
    """One instance's way out of service: its path, `None` when it was not observed, its seconds,
    and the instance, `None` when none stopped in the time the job waits."""

    path: Path | None
    seconds: float
    instance: str | None = None


def numbers_of(
    departures: Sequence[Departure], streams: Mapping[Number, bool], wakes_sent: int, wakes_lost: int
) -> dict[str, int | float]:
    """The numbers the job posts. ValueError when a path has no departure: the run is not measured."""
    numbers: dict[str, int | float] = {}
    for path, name in LEAVE_NUMBER.items():
        seconds = [departure.seconds for departure in departures if departure.path is path]
        if not seconds:
            raise ValueError(f"no departure took the path {path}")
        numbers[name] = max(seconds)
    for name, completed in streams.items():
        numbers[name] = int(completed)
    numbers[Number.WAKES_SENT] = wakes_sent
    numbers[Number.WAKES_LOST] = wakes_lost
    return numbers


# A gateway deployment.


def rollout_at(before: set[str], tasks: Mapping[str, tuple[float, float]]) -> float | None:
    """When the first gateway task not in `before` first wrote, from each task's first and last
    write: `None` when none did."""
    return min((first for task, (first, _) in tasks.items() if task not in before), default=None)


def across_rollout(completed: bool, spanned: bool, rolled_out_at: float | None) -> bool:
    """The stream across a gateway deployment, as a number: completed after it was held through the
    rollout (`spanned`), or cut while a new task wrote (`rolled_out_at`). ValueError otherwise: no
    deployment was seen under the stream, and the run is not measured."""
    if completed and spanned:
        return True
    if not completed and rolled_out_at is not None:
        return False
    raise ValueError("no gateway deployment was seen while the stream was held")


# The pool, as the workspace and the pool-state table show it.


@dataclass(frozen=True)
class Pool:
    """The pool under test, read from the workspace and the pool-state table."""

    name: str
    workspace: common.Workspace
    table: Any

    def note(self) -> ScalerNote | None:
        """The scaler's note, read strongly consistent."""
        item = self.table.get_item(Key={"pool": self.name, "record": NOTE_RECORD}, ConsistentRead=True).get("Item")
        return None if item is None else ScalerNote.from_item(item)

    def asked(self) -> int | None:
        note = self.note()
        return None if note is None else note.capacity_asked

    def gauge(self, name: str) -> int | None:
        """The scaler's last reading of the gauge `name`, `None` when it wrote none in `FRESH_SECONDS`.
        A range query gives each step the last sample before it: when the scaler wrote it is read
        from `timestamp()`."""
        end = time.time()
        selector = f'{name}{{pool="{self.name}"}}'
        stamps = self.workspace.read_series(f"timestamp({selector})", start=end - FRESH_SECONDS, end=end, step=15)
        if max((value for one in stamps for value in one.values()), default=0.0) < end - FRESH_SECONDS:
            return None
        values = [
            one.samples[-1][1]
            for one in self.workspace.read_series(selector, start=end - FRESH_SECONDS, end=end, step=15)
            if one.samples
        ]
        return round(values[-1]) if values else None

    def warm_counts(self, start: float, end: float) -> list[tuple[float, float]]:
        """The scaler's readings of the warm count from `start` to `end`, each at the time it wrote
        it: a range query repeats a reading at every step after it, `timestamp()` says when it was."""
        selector = f'{WARM_INSTANCES}{{pool="{self.name}"}}'
        values = dict(
            sample
            for one in self.workspace.read_series(selector, start=start, end=end, step=15)
            for sample in one.samples
        )
        stamps = self.workspace.read_series(f"timestamp({selector})", start=start, end=end, step=15)
        return sorted({(stamp, values[step]) for one in stamps for step, stamp in one.samples if step in values})

    def writes(self, start: float, end: float) -> dict[str, float]:
        """Each instance of the pool that wrote from `start` to `end`, with its last write."""
        query = f'max by (instance) (timestamp(up{{pool="{self.name}"}}))'
        return last_writes(self.workspace.read_series(query, start=start, end=end, step=15))

    def gateway_tasks(self, start: float, end: float) -> dict[str, tuple[float, float]]:
        """Each gateway task that wrote from `start` to `end`, with its first and last write."""
        query = f'max by (instance) (timestamp(up{{job="{GATEWAY_JOB}"}}))'
        return spans(self.workspace.read_series(query, start=start, end=end, step=15))


# The requests.


def _request(model: str) -> loadgen.Request:
    """A short streamed Messages request with a fresh conversation."""
    session = loadgen.SyntheticSession(model, START_TOKENS, (), 64, secrets.randbits(48))
    return next(session.requests())[1]


async def _completes(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    content: bytes | AsyncIterator[bytes],
    transport: httpx.AsyncBaseTransport | None,
) -> bool:
    """Sends `request` with `content` as its body, as the student whose key is given, and says
    whether it completed: answered 200, with its closing event and no error event. Raises httpx's
    errors."""
    headers = {name: value for name, value in request.headers.items() if name.lower() != "content-length"}
    headers["authorization"] = f"Bearer {key}"
    reader, closed, failed = SseReader(), False, False
    async with (
        httpx.AsyncClient(base_url=endpoint, timeout=loadgen.TIMEOUT, transport=transport, trust_env=False) as http,
        http.stream(request.method, request.path, headers=headers, content=content) as answer,
    ):
        if answer.status_code != HTTPStatus.OK:
            return False
        async for chunk in answer.aiter_bytes():
            for event in reader.feed(chunk):
                closed = closed or event.name == CLOSING_EVENT
                failed = failed or event.name == ERROR_EVENT
    return closed and not failed


async def slow_stream(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    seconds: float,
    *,
    until: Callable[[], Awaitable[bool]] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Sends `request` as a held stream: its body but the last byte, then a byte of padding every
    `TRICKLE_SECONDS` for `seconds`, or until `until` says that the event the stream spans is over,
    then the last byte. Says whether it completed (`_completes`)."""
    body = request.body
    if not body.endswith(b"}"):
        raise ValueError("a held stream's body is a JSON object")
    steps = max(1, math.ceil(seconds / TRICKLE_SECONDS))

    async def held() -> AsyncIterator[bytes]:
        yield body[:-1]
        for _ in range(steps):
            if until is not None and await until():
                break
            await sleep(seconds / steps)
            yield PADDING
        yield body[-1:]

    try:
        return await _completes(endpoint, key, request, held(), transport)
    except httpx.HTTPError:
        return False


async def wake(
    endpoint: str, key: str, model: str, bound: float, *, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """Sends one streamed request as the student whose key is given: whether it completed within
    `bound` seconds. A refusal, an error event, a cut connection or no end in time is a lost wake."""
    request = _request(model)
    try:
        async with asyncio.timeout(bound):
            return await _completes(endpoint, key, request, request.body, transport)
    except (TimeoutError, httpx.HTTPError):
        return False


@dataclass(frozen=True)
class HeldStream:
    """A session that sends one short stream after another, until `stop` is set."""

    request: loadgen.Request
    stop: asyncio.Event

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        while not self.stop.is_set():
            yield 0.0, self.request


async def _wait(condition: Callable[[], bool], seconds: float, what: str, every: float = POLL_SECONDS) -> None:
    deadline = time.monotonic() + seconds
    while not await asyncio.to_thread(condition):
        if time.monotonic() > deadline:
            raise TimeoutError(f"waited {seconds:.0f} seconds for {what}")
        await asyncio.sleep(every)


@dataclass
class Run:
    """One run of the spike against the pool, through the gateway at `endpoint`. `transport` is for
    the tests: the job gives none."""

    pool: Pool
    endpoint: str
    keys: Sequence[str]
    settings: config.Pool
    wakes: int
    deployment_hold_seconds: float
    transport: httpx.AsyncBaseTransport | None = None
    departures: list[Departure] = field(default_factory=list, init=False)

    @property
    def student(self) -> str:
        """The canary student of the held streams and the wakes; the load runs as the others."""
        return self.keys[0]

    async def across_deployment(self) -> bool:
        """Holds a stream across a gateway deployment (`across_rollout`): whether it completed."""
        began = time.time()
        before = set(await asyncio.to_thread(self.pool.gateway_tasks, began - FRESH_SECONDS, began))
        if not before:
            raise RuntimeError("no gateway task was writing when the stream was opened")
        seen: list[float] = []
        spanned = False

        async def rolled_out() -> bool:
            nonlocal spanned
            now = time.time()
            at = rollout_at(before, await asyncio.to_thread(self.pool.gateway_tasks, began, now))
            seen[:] = [] if at is None else [at]
            spanned = at is not None and now >= at + ROLLOUT_MARGIN_SECONDS
            return spanned

        start = time.strftime("%H:%M:%S UTC", time.gmtime(began))
        print(f"holding a stream for up to {self.deployment_hold_seconds:.0f} s from {start}: release the pipeline")
        request = _request(self.pool.name)
        completed = await slow_stream(
            self.endpoint,
            self.student,
            request,
            self.deployment_hold_seconds,
            until=rolled_out,
            transport=self.transport,
        )
        if not completed and not seen:
            # Cut before the job looked again: a task that first wrote while it was open is a rollout under it.
            at = rollout_at(before, await asyncio.to_thread(self.pool.gateway_tasks, began, time.time()))
            seen[:] = [] if at is None else [at]
        try:
            survived = across_rollout(completed, spanned, seen[0] if seen else None)
        except ValueError:
            message = "no gateway deployment was seen under the stream: release the pipeline while it is held"
            raise RuntimeError(message) from None
        print(f"the stream across the gateway deployment {'completed' if survived else 'did not complete'}")
        return survived

    async def two_instances(self) -> None:
        """Holds the load until the scaler asks for two instances and reads two in service."""
        streams = 2 * self.settings.target_concurrency
        load_keys = self.keys[1:]
        if len(load_keys) < math.ceil(streams / STREAMS_PER_STUDENT):
            raise ValueError("the load needs more canary students")
        stop = asyncio.Event()
        load = loadgen.Load(
            tuple(HeldStream(_request(self.pool.name), stop) for _ in range(streams)),
            sessions_per_student=STREAMS_PER_STUDENT,
        )
        held = asyncio.create_task(loadgen.replay(load, load_keys, self.endpoint, at_once=streams))

        def two() -> bool:
            if held.done():
                raise RuntimeError("the load ended before two instances served: a held stream was refused")
            return self.pool.asked() == INSTANCES and self.pool.gauge(INSTANCES_IN_SERVICE) == INSTANCES

        try:
            await _wait(two, SCALE_OUT_WAIT_SECONDS, "two instances in service")
        finally:
            stop.set()
            await held
        print("two instances in service: the load has stopped")

    async def departure(self, asked: int, wait: float, after: asyncio.Task[Departure] | None = None) -> Departure:
        """Waits until the scaler asks for fewer than `asked` instances, then until one of those
        writing at that moment has stopped, and returns its departure. The instance of the departure
        `after`, the one before, is not among them: it may still have been writing a moment before.

        The scaler writes its note on every run, so the note's `written_at` is when it began to ask
        for fewer only for the first note that does: the watch starts while the scaler still asks
        for `asked`, and reads the note every `NOTE_POLL_SECONDS`, well within one of its runs."""
        notes: list[ScalerNote] = []

        def fewer() -> bool:
            note = self.pool.note()
            notes[:] = [] if note is None else [note]
            return bool(notes) and notes[0].capacity_asked < asked

        await _wait(fewer, wait, f"the scaler to ask for fewer than {asked}", NOTE_POLL_SECONDS)
        asked_at = float(notes[0].written_at)
        print(f"the scaler asked for {notes[0].capacity_asked} instance(s)")
        earlier = None if after is None else (await after).instance
        before = await asyncio.to_thread(self.pool.writes, asked_at - FRESH_SECONDS, asked_at)
        writing = {
            instance for instance, at in before.items() if at >= asked_at - QUIET_SECONDS and instance != earlier
        }
        if not writing:
            raise RuntimeError("no instance of the pool was writing when the scaler asked for fewer")
        gone: dict[str, float] = {}

        def left() -> bool:
            now = time.time()
            gone.update(stopped(self.pool.writes(asked_at - FRESH_SECONDS, now), writing, now))
            return bool(gone) or now - asked_at > LEAVE_WAIT_SECONDS

        await _wait(left, LEAVE_WAIT_SECONDS + 2 * QUIET_SECONDS, "an instance to stop")
        now = time.time()
        if not gone:
            print(f"no instance stopped in {LEAVE_WAIT_SECONDS} s: the departure is not measured")
            return Departure(None, now - asked_at)
        instance = min(gone, key=gone.__getitem__)
        seconds = leave_seconds(asked_at, gone[instance])
        since = await asyncio.to_thread(self.pool.writes, asked_at, now)
        warm = warm_around(
            await asyncio.to_thread(self.pool.warm_counts, asked_at - FRESH_SECONDS, now),
            asked_at,
            gone[instance] + SCRAPE_SECONDS,
        )
        path = None if warm is None else path_of(*warm, newcomer=bool(set(since) - set(before)))
        print(f"an instance left in {seconds:.0f} s, path {path or 'not told'}, warm {warm or 'not read'}")
        return Departure(path, seconds, instance)

    async def cycle(self, *, scale_in_stream: bool) -> bool | None:
        """Two instances, then both departures. With `scale_in_stream`, a stream is held from while
        the scaler asks for two until the first departure is over: whether it completed."""
        await self.two_instances()
        # Both departures are watched from now, while the scaler still asks for two: each sees the
        # first note that asks for fewer.
        idle = self.settings.idle_minutes * 60
        first = asyncio.create_task(self.departure(INSTANCES, SCALE_IN_WAIT_SECONDS))
        last = asyncio.create_task(self.departure(1, SCALE_IN_WAIT_SECONDS + LEAVE_WAIT_SECONDS + idle, first))
        streaming = None
        if scale_in_stream:

            async def over() -> bool:
                return first.done()

            request = _request(self.pool.name)
            streaming = asyncio.create_task(
                slow_stream(
                    self.endpoint, self.student, request, SCALE_IN_HOLD_SECONDS, until=over, transport=self.transport
                )
            )
        completed = None
        try:
            self.departures.append(await first)
        finally:
            if streaming is not None:
                completed = await streaming
        self.departures.append(await last)
        return completed

    def _at_zero(self) -> bool:
        return self.pool.asked() == 0 and self.pool.gauge(INSTANCES_IN_SERVICE) == 0

    async def send_wakes(self) -> tuple[int, int]:
        """Sends `wakes` requests, each at the moment the pool reaches zero after its idle period:
        the wakes sent and lost. Before each, the pool is brought back if the last request did not
        complete, by requests that are not counted."""
        bound = self.settings.hold_seconds + WAKE_MARGIN_SECONDS
        wait = self.settings.idle_minutes * 60 + SCALE_IN_WAIT_SECONDS
        awake, lost = False, 0
        for sent in range(1, self.wakes + 1):
            for _ in range(RECOVERIES):
                if awake:
                    break
                awake = await wake(self.endpoint, self.student, self.pool.name, bound, transport=self.transport)
            if not awake:
                print(f"the pool could not be brought back: {sent - 1} wake(s) sent")
                return sent - 1, lost
            await _wait(lambda: self.pool.asked() != 0, SCALE_OUT_WAIT_SECONDS, "the pool to wake", NOTE_POLL_SECONDS)
            await _wait(self._at_zero, wait, "the pool to reach zero", NOTE_POLL_SECONDS)
            awake = await wake(self.endpoint, self.student, self.pool.name, bound, transport=self.transport)
            lost += not awake
            print(f"wake {sent}: {'answered' if awake else 'lost'}")
        return self.wakes, lost

    async def measure(self) -> dict[str, int | float]:
        deployment = await self.across_deployment()
        scale_in = await self.cycle(scale_in_stream=True)
        print(f"the stream across the scale-in {'completed' if scale_in else 'did not complete'}")
        for _ in range(CYCLES - 1):
            if {departure.path for departure in self.departures} >= set(Path):
                break
            await self.cycle(scale_in_stream=False)
        streams = {
            Number.STREAM_COMPLETED_SCALE_IN: bool(scale_in),
            Number.STREAM_COMPLETED_GATEWAY_DEPLOYMENT: deployment,
        }
        # Checked before the wakes, which take the pool's idle period each.
        numbers_of(self.departures, streams, 0, 0)
        sent, lost = await self.send_wakes()
        return numbers_of(self.departures, streams, sent, lost)


def main(environ: Mapping[str, str] = os.environ) -> None:
    """Runs the spike with the job's environment and credentials, and posts its numbers."""

    def measure() -> dict[str, int | float]:
        name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
        (settings,) = [pool for pool in config.read_ssm().pools if pool.name == name]
        wakes = int(environ.get(WAKES_VARIABLE, DEFAULT_WAKES))
        hold = float(environ.get(DEPLOYMENT_HOLD_VARIABLE, DEFAULT_DEPLOYMENT_HOLD_SECONDS))
        if wakes < 1 or hold <= 0:
            raise ValueError("the job sends one wake or more, and holds its stream for some time")
        session = boto3.Session()
        workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
        table = session.resource("dynamodb").Table(environ[POOL_STATE_TABLE_VARIABLE])
        keys = common.canary_keys().students
        run = Run(Pool(name, workspace, table), environ[ENDPOINT_VARIABLE], keys, settings, wakes, hold)
        return asyncio.run(run.measure())

    common.run_spike(SPIKE, JOB, measure, environ=environ)


if __name__ == "__main__":
    main()
