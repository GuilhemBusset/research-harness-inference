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

1. **The stream across a gateway deployment.** It holds one slow stream open for
   `RHI_S7_DEPLOYMENT_HOLD_SECONDS` and prints when the hold begins and how long it lasts. The owner
   starts the deployment, by releasing the pipeline again once the job has started: the job starts
   none. It records whether the stream completed.
2. **Two instances.** It holds twice the pool's `target_concurrency` of short streams until the
   scaler asks for two instances and reads two in service, then stops the load.
3. **The departures.** It sends nothing more but one slow stream after another, as one canary
   student, and watches the scaler's note. Each time the note asks for fewer instances, the scaler
   has chosen an idle one to leave: its `written_at` is when the leave began. The slow stream open
   when the job saw the first such note is the stream across a scale-in; once it ends, no other is
   sent, so that the pool can go idle and the scaler take it to zero.
4. **The wakes.** It sends one request to bring the pool back, then, as many times as
   `RHI_S7_WAKES` gives, waits until the scaler's note asks for no instance after the pool's idle
   period, and sends one request at once. A wake is lost when its request is not answered 200
   within the pool's hold and `WAKE_MARGIN_SECONDS`. Each answered wake brings the pool back for
   the next; a lost one ends the wakes, since a pool it did not wake does not reach zero again.

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
`written_at` to its last write, plus one scrape, by which it had stopped. One that writes for
`LEAVE_WAIT_SECONDS` after the note has not left in that time, and the seconds waited are its
number: far above the pass condition.

**Which path it took** (`path_of`), from the warm count, which counts an instance going back to
the warm pool from its first wait state: the warm count rose after the note and no instance new to
the pool wrote, so the instance went back to the warm pool; the warm count did not rise, so it was
terminated. A warm count that rose while a new instance wrote may be a fresh instance launched
into the warm pool in place of a terminated one: that departure has no path, and is not a number.
A run whose departures did not take both paths takes the pool to two instances again, up to
`CYCLES` times, and is not measured when a path is still missing. A path seen twice keeps its
longer time.

**The slow stream** (`slow_stream`). The mock streams its canned turn in well under a second, so a
stream is made slow by its reader: it reads the answer through a socket with a small receive
buffer and pauses after each output token, so that the whole answer takes the seconds given. It
completed when it was answered 200 and its last event, `message_stop`, arrived.

**The numbers** (`Number`): the seconds to leave service on each path, whether each held stream
completed (1 or 0), and the wakes sent and lost.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s7_drain`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |
| `RHI_S7_WAKES` | The wakes it sends, each at the moment the pool reaches zero; `DEFAULT_WAKES` unless given |
| `RHI_S7_DEPLOYMENT_HOLD_SECONDS` | How long the stream across the gateway deployment is held; `DEFAULT_DEPLOYMENT_HOLD_SECONDS` unless given |
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
import socket
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
from rhi_mock_engine.app import ANSWER, REASONING
from rhi_scaler.metrics import INSTANCES_IN_SERVICE, WARM_INSTANCES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence

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
# Seconds. The node's collector scrapes, and writes `up`, every 15 seconds; an instance whose last
# write is this old has stopped.
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
# Seconds a slow stream across a scale-in is read over.
SLOW_STREAM_SECONDS = 120
# Seconds a wake may take beyond the pool's hold before it counts as lost.
WAKE_MARGIN_SECONDS = 120
# The streams one student of the load holds: one under a student's limit (as in S8).
STREAMS_PER_STUDENT = DEFAULT_CONCURRENCY_LIMIT - 1
# Bytes: the receive buffer of a slow stream's socket, so that the reader's pace reaches the sender.
RECEIVE_BUFFER_BYTES = 1024
# The output tokens of the mock's canned turn: a slow stream pauses after each.
OUTPUT_TOKENS = len(REASONING) + len(ANSWER)
START_TOKENS = 200
CLOSING_EVENT = "message_stop"


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
    written: dict[str, float] = {}
    for one in series:
        instance = one.labels.get("instance")
        if instance and one.samples:
            written[instance] = max(written.get(instance, 0.0), *one.values())
    return written


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


@dataclass(frozen=True)
class Departure:
    """One instance's way out of service: its path, `None` when it cannot be told, its seconds, and
    the instance, `None` when none stopped in the time the job waits."""

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
        """The warm count's samples from `start` to `end`."""
        series = self.workspace.read_series(f'{WARM_INSTANCES}{{pool="{self.name}"}}', start=start, end=end, step=15)
        return [sample for one in series for sample in one.samples]

    def writes(self, start: float, end: float) -> dict[str, float]:
        """Each instance of the pool that wrote from `start` to `end`, with its last write."""
        query = f'max by (instance) (timestamp(up{{pool="{self.name}"}}))'
        return last_writes(self.workspace.read_series(query, start=start, end=end, step=15))


def warm_around(samples: Sequence[tuple[float, float]], at: float) -> tuple[int, int]:
    """The warm count at `at`, the last sample at or before it, and the highest after it."""
    before = [value for moment, value in samples if moment <= at]
    after = [value for moment, value in samples if moment > at]
    first = round(before[-1]) if before else 0
    return first, max([first, *(round(value) for value in after)])


# The requests.


def _request(model: str) -> loadgen.Request:
    """A short streamed Messages request with a fresh conversation."""
    session = loadgen.SyntheticSession(model, START_TOKENS, (), 64, secrets.randbits(48))
    return next(session.requests())[1]


async def slow_stream(
    endpoint: str,
    key: str,
    request: loadgen.Request,
    seconds: float,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Sends `request` as the student whose key is given, reads its answer pausing after each
    output token so that it takes about `seconds`, and says whether it completed: answered 200,
    with its closing event."""
    pause = seconds / OUTPUT_TOKENS
    if transport is None:
        options = [(socket.SOL_SOCKET, socket.SO_RCVBUF, RECEIVE_BUFFER_BYTES)]
        transport = httpx.AsyncHTTPTransport(socket_options=options)
    headers = {**request.headers, "authorization": f"Bearer {key}"}
    reader, closed = SseReader(), False
    try:
        async with (
            httpx.AsyncClient(base_url=endpoint, timeout=loadgen.TIMEOUT, transport=transport, trust_env=False) as http,
            http.stream(request.method, request.path, headers=headers, content=request.body) as answer,
        ):
            if answer.status_code != HTTPStatus.OK:
                return False
            async for chunk in answer.aiter_raw():
                for event in reader.feed(chunk):
                    if loadgen.output_of(loadgen.Wire.MESSAGES, event) is not None:
                        await sleep(pause)
                    closed = closed or event.name == CLOSING_EVENT
    except httpx.HTTPError:
        return False
    return closed


async def wake(endpoint: str, key: str, model: str, bound: float) -> bool:
    """Sends one streamed request as the student whose key is given: whether it was answered 200
    within `bound` seconds."""
    load = loadgen.Load((loadgen.SyntheticSession(model, START_TOKENS, (), 64, secrets.randbits(48)),))
    try:
        records = await asyncio.wait_for(loadgen.replay(load, [key], endpoint, at_once=1), bound)
    except (TimeoutError, httpx.HTTPError):
        return False
    return len(records) == 1 and records[0].status == HTTPStatus.OK


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
    """One run of the spike against the pool, through the gateway at `endpoint`."""

    pool: Pool
    endpoint: str
    keys: Sequence[str]
    settings: config.Pool
    wakes: int
    deployment_hold_seconds: float
    departures: list[Departure] = field(default_factory=list, init=False)

    @property
    def student(self) -> str:
        """The canary student of the held streams and the wakes; the load runs as the others."""
        return self.keys[0]

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
        instance = min(gone, key=gone.__getitem__) if gone else None
        seconds = now - asked_at if instance is None else leave_seconds(asked_at, gone[instance])
        since = await asyncio.to_thread(self.pool.writes, asked_at, now)
        warm_before, warm_after = warm_around(
            await asyncio.to_thread(self.pool.warm_counts, asked_at - FRESH_SECONDS, now), asked_at
        )
        path = path_of(warm_before, warm_after, newcomer=bool(set(since) - set(before)))
        print(f"an instance left in {seconds:.0f} s, path {path or 'not told'}, warm {warm_before} to {warm_after}")
        return Departure(path, seconds, instance)

    async def cycle(self, scale_in_stream: bool) -> bool | None:
        """Two instances, then both departures. With `scale_in_stream`, slow streams are held until
        the first departure begins: whether the one open then completed."""
        await self.two_instances()
        # Both departures are watched from now, while the scaler still asks for two: each sees the
        # first note that asks for fewer.
        idle = self.settings.idle_minutes * 60
        first = asyncio.create_task(self.departure(INSTANCES, SCALE_IN_WAIT_SECONDS))
        last = asyncio.create_task(self.departure(1, SCALE_IN_WAIT_SECONDS + LEAVE_WAIT_SECONDS + idle, first))
        stop, results = asyncio.Event(), []

        async def streams() -> None:
            while not stop.is_set():
                request = _request(self.pool.name)
                results.append(await slow_stream(self.endpoint, self.student, request, SLOW_STREAM_SECONDS))

        streaming = asyncio.create_task(streams()) if scale_in_stream else None
        try:
            await _wait(lambda: self.pool.asked() != INSTANCES, SCALE_IN_WAIT_SECONDS, "a scale-in", NOTE_POLL_SECONDS)
        finally:
            # The stream open now is the one across the scale-in; none follows it, so that the pool
            # can go idle.
            stop.set()
            if streaming is not None:
                await streaming
        self.departures.append(await first)
        self.departures.append(await last)
        return results[-1] if results else None

    async def send_wakes(self) -> tuple[int, int]:
        """Sends up to `wakes` requests, each at the moment the scaler asks for no instance after
        the pool's idle period: the wakes sent and lost. The first lost ends them, since the pool it
        did not wake does not reach zero again."""
        bound = self.settings.hold_seconds + WAKE_MARGIN_SECONDS
        if not await wake(self.endpoint, self.student, self.pool.name, bound):
            raise RuntimeError("the request that brings the pool back before the wakes was not answered")
        wait = self.settings.idle_minutes * 60 + SCALE_IN_WAIT_SECONDS
        for sent in range(1, self.wakes + 1):
            await _wait(lambda: self.pool.asked() != 0, SCALE_OUT_WAIT_SECONDS, "the pool to wake", NOTE_POLL_SECONDS)
            await _wait(lambda: self.pool.asked() == 0, wait, "the pool to reach zero", NOTE_POLL_SECONDS)
            answered = await wake(self.endpoint, self.student, self.pool.name, bound)
            print(f"wake {sent}: {'answered' if answered else 'lost'}")
            if not answered:
                return sent, 1
        return self.wakes, 0

    async def measure(self) -> dict[str, int | float]:
        began = time.strftime("%H:%M:%S UTC", time.gmtime())
        print(f"holding a stream for {self.deployment_hold_seconds:.0f} s from {began}: release the pipeline now")
        request = _request(self.pool.name)
        deployment = await slow_stream(self.endpoint, self.student, request, self.deployment_hold_seconds)
        print(f"the stream across the gateway deployment {'completed' if deployment else 'did not complete'}")
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
