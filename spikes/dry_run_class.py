"""The dry-run class: the class's sessions for an hour on the coursework pool, recorded (system
design, sections 4.7, 5.6, 7.3, 12 and 14).

Phase 3 ends with a dry-run class on the coursework model: the expected number of concurrent
harness sessions for an hour, in a session switched on for it, within the objectives of section
7.3. This job runs the hour and **records what it observed, with times, and judges nothing**:
whether the hour met the five objectives, and how each measurement compares with the pool's
expected values, is read later, from its lines, by an issue written then (section 12).

**Before it starts**, the owner switches a session on for the rehearsal and turns the public HTTPS
listener on; the job then wakes the pool as `rhi wait` does and waits until the gateway says it is
`ready` (`Rehearsal.ready`), so that the hour measures a warm pool, not a cold start.

**The class.** The number of sessions is the class size, decision 5 of the design, which the owner
gives: the job has no default for it. A session is one student, sent as a canary student of its own
with its own key (`common.canary_keys`), for the whole hour; there are as many sessions as students,
from the hour's start to its end. The shape is the job's other argument (section 4.7):

- `hands-on`: each student runs one agent;
- `several-agents`: each student runs `loadgen.AGENTS_PER_STUDENT` agents at once, as in the second
  session, all on the student's key: parallel streams inside the one session.

Each agent replays conversations of section 4.7's sizes, as Claude Code sends them, with the load
generator (`loadgen.replay`, `loadgen.agent_session`, `s5_capacity.AsClaudeCode`), one after the
other: a conversation holds `s5_capacity.TURNS_PER_SESSION` turns, which keeps its transcript in
section 4.7's longest row, and when it ends the agent starts the next one after one pause of tools
(`loadgen.PAUSE_SECONDS`), a new task of the same student, whose first request is a first turn
again. No request is sent once the hour has passed, and the hour ends when the requests in flight
have. A conversation that ends on a request not answered 200, or that failed, is followed by the
next after `AFTER_REFUSAL_SECONDS`.

**Whether a request was held.** The gateway holds a request for a pool that is not ready, and a
held stream gets a keep-alive every few seconds until the engine's first event
(`rhi_gateway.hold`); a hold that ends before its first keep-alive is due leaves none. So the job
also reads the pool's state from the gateway's status every `POLL_SECONDS` through the hour, the
view of readiness the hold itself decides on, and writes a `pool_state` line each time it changes.
A request answered 200 is `held` when a keep-alive came before the engine's first event
(`kept_alive`), or when the status read last before it was sent, or any read after it up to its
first token, said the pool was not ready (`pool_not_ready_read`). Both are written with it.

**What it records.** One JSON object per line on standard output, which the spike jobs' log group
keeps (`s7_drain.Recorder`). Each has `at`, the UTC time to the millisecond, and `kind`; every other
time in it is a UTC time in the same form, or null when it was not seen. An instance is named by its
order of appearance in the run, `instance-1`, `instance-2`, ... (`s7_drain.Names`), never by its AWS
identifier. No key and no text that AWS or the network returned is written: an error is one of the
fixed kinds of `s7_drain.ErrorKind`, a failed request one of `loadgen.Failure`, a refusal its status
number and its cause only when that is one of the gateway's own (`rhi_common.causes.Cause`).

| Kind | When, and what it holds |
| --- | --- |
| `start` | The pool, the class size and the shape it was given, the sessions and the agents each runs, the hour's seconds |
| `pool_ready` | The pool was ready: whether the job woke it, and the seconds it waited |
| `hour_started`, `hour_ended` | The hour's bounds |
| `engine_counters` | At the hour's `start` and `end`: each instance's counters of the engine's row of the per-engine table (`rhi_common.engine_metrics`), cached and input tokens and preemptions among them where the row has them; a quantity the row leaves out is not written |
| `cache_capacity` | At the start: the cache capacity in tokens each instance's engine reported (`rhi_lifecycle.cache`) |
| `in_service` | At the start and the end: the scaler's count of the pool's instances in service, and the instances that write their release, which only one in service does (`rhi_lifecycle.serving`) |
| `pool_state` | The pool's state as the gateway's status gave it, each time it changes; `unread` when it could not be read |
| `request` | One request: its session (the student), agent and conversation, whether it was the conversation's first turn, when it was sent and its first and last token came, its input, cached and output tokens, whether it was held, its status, its cause and its failure |
| `instance_started` | An instance whose launch began during the hour: its transition and the seconds of each stage, from the lifecycle agent's metric (`rhi_lifecycle.stages`) |
| `error` | A reading that could not be made, by its step and kind; the hour goes on |
| `end` | The requests recorded |

**Which instances started during the hour.** The lifecycle agent keeps exporting the stages of its
last launch for as long as it runs, so an instance that was serving before the hour also writes
stage samples in it. The job reads the stage metric from `LOOKBACK_SECONDS` before the hour to its
end. For each instance and transition, the stages it exported at the hour's start are its launch
from before; a launch began in the hour where a sample in it gives a stage another number of
seconds than the launch before, which a new launch does: it empties the metric and sets each stage
as it ends (`launches_of`).

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-dry_run_class`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output: a host name, reached over HTTPS (`s4_weights.gateway_address`) |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_DRY_RUN_CLASS_SIZE` | The class size, in students: the sessions it replays. Required |
| `RHI_DRY_RUN_SHAPE` | `hands-on` or `several-agents`. Required |

The pool is the one named `coursework` (`POOL`), always; its engine comes from the deployed
configuration (`rhi.config.read_ssm`), and the region from CodeBuild's `AWS_REGION`. The job refuses,
before it sends anything, a class size that is missing or below one, a shape of another name, and a
class larger than the canary students' keys.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import os
import sys
from dataclasses import dataclass, field
from random import Random
from typing import TYPE_CHECKING, Any, Protocol

import boto3
import httpx

import common
import loadgen
import s4_weights
from rhi import config
from rhi_common.engine_metrics import Kind, Name, further_names, metric_names
from rhi_gateway.local import State
from rhi_lifecycle.cache import METRIC as CACHE_CAPACITY_METRIC
from rhi_lifecycle.serving import RELEASE_METRIC
from rhi_lifecycle.stages import METRIC as STAGE_METRIC
from rhi_lifecycle.stages import Stage, Transition
from rhi_scaler.metrics import INSTANCES_IN_SERVICE
from s5_capacity import TURNS_PER_SESSION, AsClaudeCode, Shape, Until
from s7_drain import Clock, Names, Recorder, SeriesReader, SystemClock, error_kind, utc

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence

JOB = "dry_run_class"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
CLASS_SIZE_VARIABLE = "RHI_DRY_RUN_CLASS_SIZE"
SHAPE_VARIABLE = "RHI_DRY_RUN_SHAPE"
# The pool the dry run rehearses: the coursework pool, and no other.
POOL = "coursework"
HOUR_SECONDS = 3600.0

# Seconds. While the pool wakes, and through the hour: its status is read this often, and the wake
# repeated this often while it is not ready, as `rhi wait` does; a cold launch may take 40 minutes
# at worst (spike S1).
POLL_SECONDS = 5.0
WAKE_EVERY_SECONDS = 60.0
READY_WAIT_SECONDS = 40 * 60.0
# Seconds before the next conversation of an agent whose conversation ended refused or failed.
AFTER_REFUSAL_SECONDS = 10.0
# Seconds. The collectors scrape every 15 seconds; the samples of the hour's last answers reach the
# workspace within a few scrapes, and the job waits that long before its end readings.
SCRAPE_SECONDS = 15.0
SETTLE_SECONDS = 4 * SCRAPE_SECONDS
# Seconds. Prometheus' lookback: how far back a reading at a time finds a series' last sample.
LOOKBACK_SECONDS = 300.0
# Seconds a series' last sample may be old and still be read as written now: four scrapes. The
# scaler's gauges are written once a minute, and may be two minutes old (`s7_drain.FRESH_SECONDS`).
WRITING_SECONDS = 4 * SCRAPE_SECONDS
SCALER_FRESH_SECONDS = 120.0
# Two stage samples of one launch give a stage the same seconds; a new launch gives it others.
_SAME_SECONDS = 1e-6

_STAGES = frozenset(stage.value for stage in Stage)
_TRANSITIONS = frozenset(transition.value for transition in Transition)
_STATES = frozenset(state.value for state in State)
UNREAD = "unread"


class Gateway(Protocol):
    """What the job asks the gateway: `s4_weights.Gateway`, or a test's."""

    def state(self) -> str:
        """The pool's state as the status gives it; empty when it could not be read."""
        ...

    def wake(self) -> None:
        """Wakes the pool, as a student's `rhi wait` does."""
        ...


def conversation_of(seed: int) -> loadgen.Session:
    """One conversation of an agent: section 4.7's sizes, drawn from `seed`, as Claude Code sends it."""
    return AsClaudeCode(loadgen.agent_session(POOL, TURNS_PER_SESSION, seed))


async def _off_loop[Result](action: Callable[[], Result]) -> Result:
    """Runs `action`, a blocking read, in a thread."""
    return await asyncio.to_thread(action)


# The stages of the launches.


type Snapshot = dict[str, float]


@dataclass(frozen=True)
class Launch:
    """One launch of an instance, as the stage metric showed it: its transition, the step it was
    first seen at, and the seconds of each stage it had ended by its last sample."""

    instance: str
    transition: str
    first_seen: float
    stages: Snapshot


def _same_launch(earlier: Snapshot, later: Snapshot) -> bool:
    """Whether two snapshots of the stages can be of one launch: no stage of both has two numbers."""
    return all(abs(earlier[stage] - later[stage]) <= _SAME_SECONDS for stage in earlier.keys() & later.keys())


def launches_of(series: Iterable[common.Series], start: float) -> list[Launch]:
    """The launches that began after `start` in the stage metric's `series`, read from before it.

    Per instance and transition, the stages of each step form a snapshot. The last one at or before
    `start` is the launch from before the hour; each later snapshot that gives a stage other seconds
    than the launch it follows begins a new one, and one that agrees with it adds the stages it ended
    since. A label outside the agent's fixed stages and transitions is not read."""
    steps: dict[tuple[str, str], dict[float, Snapshot]] = {}
    for one in series:
        instance, transition, stage = (one.labels.get(label, "") for label in ("instance", "transition", "stage"))
        if not instance or transition not in _TRANSITIONS or stage not in _STAGES:
            continue
        for at, seconds in one.samples:
            if math.isfinite(seconds):
                steps.setdefault((instance, transition), {}).setdefault(at, {})[stage] = seconds
    launches: list[Launch] = []
    for (instance, transition), snapshots in steps.items():
        current: Launch | None = None
        for at in sorted(snapshots):
            snapshot = snapshots[at]
            if at <= start:
                current = Launch(instance, transition, at, dict(snapshot))
                continue
            if current is not None and _same_launch(current.stages, snapshot):
                current.stages.update(snapshot)
                continue
            current = Launch(instance, transition, at, dict(snapshot))
            launches.append(current)
    return sorted(launches, key=lambda launch: launch.first_seen)


# The run.


@dataclass
class Rehearsal:
    """One dry-run class on the coursework pool, through the gateway at `endpoint`, as the canary
    students whose keys are given: `class_size` sessions of the shape `shape`, on the engine `engine`.

    `conversation` makes an agent's conversation from its seed, and `duration` is the hour's seconds;
    `transport`, which requests are sent through, and `offload`, which runs a blocking read off the
    event loop, are for the tests, as the first two are: the job gives none of them."""

    engine: str
    class_size: int
    shape: Shape
    keys: Sequence[str] = field(repr=False)
    endpoint: str
    gateway: Gateway
    workspace: SeriesReader
    recorder: Recorder
    clock: Clock
    conversation: Callable[[int], loadgen.Session] = conversation_of
    duration: float = HOUR_SECONDS
    transport: Any = None
    offload: Callable[[Callable[[], Any]], Awaitable[Any]] = _off_loop
    names: Names = field(default_factory=Names)
    step: str = "start"
    requests: int = 0
    _states: list[tuple[float, bool]] = field(default_factory=list)
    _seeds: Iterator[int] = field(default_factory=itertools.count)

    def __post_init__(self) -> None:
        if self.class_size < 1:
            raise ValueError("a class has one student or more")
        if self.class_size > len(self.keys):
            raise ValueError("the class has more students than there are canary students' keys")
        metric_names(self.engine)

    @property
    def agents(self) -> int:
        """The agents each session runs at once."""
        return 1 if self.shape is Shape.HANDS_ON else loadgen.AGENTS_PER_STUDENT

    # The pool.

    async def ready(self) -> None:
        """Wakes the pool, as `rhi wait` does, and waits until the gateway says it is ready.
        TimeoutError when it is not within `READY_WAIT_SECONDS`."""
        self.step = "ready"
        started = woke = self.clock.now()
        waking = (await self.offload(self.gateway.state)) != State.READY
        if waking:
            await self.offload(self.gateway.wake)
            while (await self.offload(self.gateway.state)) != State.READY:
                if self.clock.now() - started >= READY_WAIT_SECONDS:
                    raise TimeoutError("the pool was not ready in time")
                if self.clock.now() - woke >= WAKE_EVERY_SECONDS:
                    await self.offload(self.gateway.wake)
                    woke = self.clock.now()
                await self.clock.sleep(POLL_SECONDS)
        self.recorder.line("pool_ready", woke=waking, waited_seconds=round(self.clock.now() - started, 3))

    async def _watch(self, done: asyncio.Event) -> None:
        """Reads the pool's state from the gateway's status every `POLL_SECONDS` until `done`, and
        writes it each time it changes."""
        last: str | None = None
        while not done.is_set():
            state = await self.offload(self.gateway.state)
            read_at = self.clock.now()
            if state in _STATES:
                self._states.append((read_at, state == State.READY))
            else:
                state = UNREAD
            if state != last:
                self.recorder.line("pool_state", state=state, read_at=utc(read_at))
                last = state
            await self.clock.sleep(POLL_SECONDS)

    def _not_ready_read(self, sent: float, until: float) -> bool:
        """Whether the status read last at or before `sent`, or any read after it up to `until`, said
        the pool was not ready."""
        before = [ready for at, ready in self._states if at <= sent]
        during = [ready for at, ready in self._states if sent < at <= until]
        return not all(before[-1:] + during)

    # The workspace.

    async def _read(self, query: str, start: float, end: float, step: float = SCRAPE_SECONDS) -> list[common.Series]:
        return await self.offload(lambda: self.workspace.read_series(query, start=start, end=end, step=step))

    async def _per_instance(self, query: str, at: float) -> dict[str, float]:
        """The value at `at` of each instance's series of `query`, by the instance's name."""
        found: dict[str, float] = {}
        for one in await self._read(query, at, at):
            instance = one.labels.get("instance")
            values = [value for _, value in one.samples if math.isfinite(value)]
            if instance and values:
                found[self.names(instance)] = values[-1]
        return found

    def _counters(self) -> dict[str, Name]:
        """The counters of the engine's row: the contract's and the further ones it has."""
        names = {**metric_names(self.engine), **further_names(self.engine)}
        return {str(quantity): name for quantity, name in names.items() if name.kind is Kind.COUNTER}

    async def engine_counters(self, moment: str, at: float) -> None:
        """Each instance's counters of the engine's row at `at`, as they stand: one line."""
        instances: dict[str, dict[str, float]] = {}
        for quantity, name in self._counters().items():
            matchers = [f'pool="{POOL}"', *(f'{label}="{value}"' for label, value in name.labels.items())]
            query = f"sum by (instance) ({name}_total{{{','.join(matchers)}}})"
            for instance, value in (await self._per_instance(query, at)).items():
                instances.setdefault(instance, {})[quantity] = value
        self.recorder.line("engine_counters", moment=moment, read_at=utc(at), instances=instances)

    async def cache_capacity(self, at: float) -> None:
        """The cache capacity in tokens each instance's engine reported, as it stood at `at`: one line."""
        selector = f'{CACHE_CAPACITY_METRIC}{{pool="{POOL}"}}'
        query = f"max by (instance) (last_over_time({selector}[{round(LOOKBACK_SECONDS)}s]))"
        self.recorder.line("cache_capacity", read_at=utc(at), instances=await self._per_instance(query, at))

    async def in_service(self, moment: str, at: float) -> None:
        """The scaler's count of the pool's instances in service at `at`, and the instances writing
        their release then: one line."""
        gauge = f'{INSTANCES_IN_SERVICE}{{pool="{POOL}"}}'
        fresh = f"max({gauge} and timestamp({gauge}) > {at - SCALER_FRESH_SECONDS:.3f})"
        counted = [value for one in await self._read(fresh, at, at) for _, value in one.samples]
        release = f'max by (instance) (timestamp({RELEASE_METRIC}{{pool="{POOL}"}}))'
        serving = [
            instance
            for instance, written in (await self._per_instance(release, at)).items()
            if written >= at - WRITING_SECONDS
        ]
        self.recorder.line(
            "in_service",
            moment=moment,
            read_at=utc(at),
            scaler_in_service=round(counted[-1]) if counted and math.isfinite(counted[-1]) else None,
            serving=serving,
        )

    async def started_instances(self, start: float, end: float) -> None:
        """A line for each launch that began in the hour, from the stage metric (`launches_of`)."""
        series = await self._read(f'{STAGE_METRIC}{{pool="{POOL}"}}', start - LOOKBACK_SECONDS, end)
        for launch in launches_of(series, start):
            self.recorder.line(
                "instance_started",
                instance=self.names(launch.instance),
                transition=launch.transition,
                first_seen_at=utc(launch.first_seen),
                stages=launch.stages,
            )

    async def _guarded(self, step: str, reading: Awaitable[None]) -> None:
        """Makes one reading; one that fails is written as an `error` line, and the run goes on."""
        self.step = step
        try:
            await reading
        except Exception as error:  # noqa: BLE001 - recorded by its kind: no text of AWS's reaches the log
            self.recorder.line("error", step=step, error=error_kind(error).value)

    # The class.

    def _request_line(self, session: int, agent: int, conversation: int, record: loadgen.Record) -> None:
        sent = record.sent_at if record.sent_at is not None else self.clock.now()

        def at(seconds: float | None) -> str | None:
            return None if seconds is None else utc(sent + seconds)

        until = sent + record.first_token_seconds if record.first_token_seconds is not None else self.clock.now()
        accepted = record.status == httpx.codes.OK
        not_ready = accepted and self._not_ready_read(sent, until)
        self.requests += 1
        self.recorder.line(
            "request",
            session=session,
            agent=agent,
            conversation=conversation,
            first_turn=record.first_turn,
            sent_at=utc(sent),
            first_token_at=at(record.first_token_seconds),
            first_answer_at=at(record.first_answer_seconds),
            last_token_at=at(record.last_token_seconds),
            input_tokens=record.input_tokens,
            cached_input_tokens=record.cached_input_tokens,
            output_tokens=record.output_tokens,
            held=accepted and (record.kept_alive or not_ready),
            kept_alive=record.kept_alive,
            pool_not_ready_read=not_ready,
            status=record.status,
            cause=record.cause,
            failure=None if record.failure is None else record.failure.value,
        )

    async def _agent(self, session: int, agent: int, deadline: float) -> None:
        """One agent of one session: conversations one after the other until the deadline."""

        def sending() -> bool:
            return self.clock.now() < deadline

        for conversation in itertools.count():
            seed = next(self._seeds)
            if conversation:
                # The next task starts after a pause of tools, as a turn does.
                await self.clock.sleep(Random(seed).uniform(*loadgen.PAUSE_SECONDS))
            if not sending():
                return
            load = loadgen.Load((Until(self.conversation(seed), deadline, self.clock.now),), sending=sending)
            records = await loadgen.replay(
                load,
                [self.keys[session]],
                self.endpoint,
                at_once=1,
                clock=self.clock.now,
                sleep=self.clock.sleep,
                transport=self.transport,
                keep_failures=True,
                on_record=lambda record, conversation=conversation: self._request_line(
                    session, agent, conversation, record
                ),
            )
            if records and (records[-1].failure is not None or records[-1].status != httpx.codes.OK):
                await self.clock.sleep(AFTER_REFUSAL_SECONDS)

    async def replay(self) -> tuple[float, float]:
        """The class for the hour: every agent of every session, while the pool's state is watched.
        Returns when the hour started and when its last request ended."""
        self.step = "replay"
        start = self.clock.now()
        deadline = start + self.duration
        self.recorder.line("hour_started", started_at=utc(start), ends_at=utc(deadline))
        done = asyncio.Event()
        async with asyncio.TaskGroup() as group:
            watch = group.create_task(self._watch(done))
            async with asyncio.TaskGroup() as agents:
                for session in range(self.class_size):
                    for agent in range(self.agents):
                        agents.create_task(self._agent(session, agent, deadline))
            done.set()
            await watch
        end = self.clock.now()
        self.recorder.line("hour_ended", ended_at=utc(end), requests=self.requests)
        return start, end

    async def run(self) -> None:
        """The pool made ready, the start readings, the hour, then the end readings and the launches."""
        self.recorder.line(
            "start",
            pool=POOL,
            class_size=self.class_size,
            shape=self.shape.value,
            sessions=self.class_size,
            agents_per_session=self.agents,
            duration_seconds=self.duration,
        )
        await self.ready()
        at = self.clock.now()
        await self._guarded("start_counters", self.engine_counters("start", at))
        await self._guarded("cache_capacity", self.cache_capacity(at))
        await self._guarded("start_in_service", self.in_service("start", at))
        start, end = await self.replay()
        self.step = "settle"
        await self.clock.sleep(SETTLE_SECONDS)
        await self._guarded("end_counters", self.engine_counters("end", end + SCRAPE_SECONDS))
        await self._guarded("end_in_service", self.in_service("end", end))
        await self._guarded("started_instances", self.started_instances(start, end))
        self.recorder.line("end", requests=self.requests)


def class_size_of(environ: Mapping[str, str]) -> int | None:
    """The class size the owner gave, or None when it is missing or not a whole number of one or more."""
    given = environ.get(CLASS_SIZE_VARIABLE, "").strip()
    return int(given) if given.isdigit() and int(given) >= 1 else None


def shape_of(environ: Mapping[str, str]) -> Shape | None:
    """The shape the owner gave, or None for any other name."""
    given = environ.get(SHAPE_VARIABLE, "").strip()
    return Shape(given) if given in {shape.value for shape in Shape} else None


def main(environ: Mapping[str, str] = os.environ, clock: Clock | None = None) -> int:
    """Runs the dry-run class with the job's environment and credentials, writing its lines on
    standard output: 0 once the hour and its readings have ended, 1 when it was refused, could not
    run or failed, with a `refused` or `failed` line and no traceback."""
    clock = SystemClock() if clock is None else clock
    recorder = Recorder(clock)
    class_size, shape = class_size_of(environ), shape_of(environ)
    if class_size is None or shape is None:
        recorder.line("refused", reason="no_class_size" if class_size is None else "no_shape")
        return 1
    step = "configuration"
    rehearsal: Rehearsal | None = None
    try:
        pools = [pool for pool in config.read_ssm().pools if pool.name == POOL]
        if len(pools) != 1:
            recorder.line("refused", reason="no_coursework_pool")
            return 1
        endpoint = s4_weights.gateway_address(environ[ENDPOINT_VARIABLE])
        step = "clients"
        session = boto3.Session()
        workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
        step = "canary_keys"
        keys = common.canary_keys().students
        if len(keys) < class_size:
            recorder.line("refused", reason="too_few_keys", class_size=class_size, keys=len(keys))
            return 1
        rehearsal = Rehearsal(
            engine=pools[0].engine,
            class_size=class_size,
            shape=shape,
            keys=keys,
            endpoint=endpoint,
            gateway=s4_weights.Gateway(endpoint, POOL, keys[0]),
            workspace=workspace,
            recorder=recorder,
            clock=clock,
        )
        asyncio.run(rehearsal.run())
    except Exception as error:  # noqa: BLE001 - recorded by its kind: no text of AWS's reaches the log
        recorder.line("failed", step=step if rehearsal is None else rehearsal.step, error=error_kind(error).value)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
