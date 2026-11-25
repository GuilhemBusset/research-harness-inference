"""The dry-run class: the class's sessions for an hour on the coursework pool, recorded (system
design, sections 4.7, 5.6, 7.3, 12 and 14).

Phase 3 ends with a dry-run class on the coursework model: the expected number of concurrent
harness sessions for an hour, in a session switched on for it, within the objectives of section
7.3. This job runs the hour and **records what it observed, with times, and judges nothing**:
whether the hour met the five objectives, and how each measurement compares with the pool's
expected values, is read later, from its lines, by an issue written then (section 12).

**Before it starts**, the owner books a session of the pool for the rehearsal and turns the public
HTTPS listener on. The job starts no machine: before it sends anything it checks the session
(`common.require_session`): it is active, it has the instances the rehearsal needs in service, and
it lasts the run's minutes from now. Otherwise the job writes `refused` with the reason,
`no-session`, `too-few-instances` or `session-too-short`, sends nothing to the gateway, and exits 1.
It then waits until the gateway says the pool is `ready` (`Rehearsal.ready`), so that the hour
measures a pool that serves.

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
next after `AFTER_REFUSAL_SECONDS`. A request that failed, or whose stream carried the gateway's
error event, ends its conversation as a refusal does: the next one opens with a first turn.

**Whether a request was held** is the gateway's own decision, which its answer carries in the
header `rhi_gateway.hold.HELD_HEADER` (`loadgen.Record.held`), whether or not the hold lasted long
enough for a keep-alive. A keep-alive in the stream decides nothing: an engine may open its own
stream with a comment or a `ping`, which a warm request then gets too.

**The hour's bounds.** The hour starts once the pool is ready and ends `duration` seconds later,
both written in `hour_started`. The readings of the start and the end are of the workspace as it
stood at those two moments, whenever they are made: the start's while the class begins, the end's
once the answers in flight have ended and `SETTLE_SECONDS` have let their samples arrive.

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
| `pool_ready` | The pool was ready: the seconds the job waited for it |
| `hour_started`, `hour_ended` | The hour's bounds |
| `engine_counters` | At the hour's `start` and `end`: each instance's counters of the engine's row of the per-engine table (`rhi_common.engine_metrics`), cached and input tokens and preemptions among them where the row has them; a quantity the row leaves out is not written |
| `cache_capacity` | At the start: the cache capacity in tokens each instance's engine reported (`rhi_lifecycle.cache`) |
| `in_service` | At the start and the end: the scaler's count of the pool's instances in service, and the instances that write their release, which only one in service does (`rhi_lifecycle.serving`) |
| `request` | One request: its session (the student), agent and conversation, whether it was the conversation's first turn, when it was sent and its first and last token came, its input, cached and output tokens, whether it was held, its status, its cause and its failure |
| `instance_started` | An instance whose launch may have begun during the hour: its transition, when it began at the latest and, where an observation bounds it, at the earliest, whether that is in the hour for certain, and the seconds of each stage, from the lifecycle agent's metric (`rhi_lifecycle.stages`) |
| `error` | A reading that could not be made, by its step, its quantity for a counter, and its kind; the hour goes on, and the counters read are written with the names of those that were not (`unread`) |
| `end` | The requests recorded |

**Which instances started during the hour.** The lifecycle agent keeps exporting the stages of its
last launch for as long as it runs, so an instance that was serving before the hour also writes
stage samples in it. The job reads the stage metric from `LOOKBACK_SECONDS` before the hour to the
moment it reads. For each instance, the stages it exported at the hour's start are its launch from
before; a new launch shows where a sample gives a stage another number of seconds than the launch
before, which a new launch does: it empties the metric and sets each stage as it ends. Its samples
are taken whatever their transition label, because the agent takes a launch into service for a
promotion until it finds no image on the disk, and then moves its stages to `cold-launch`: the
launch is written once, under its last label (`launches_of`). It began, at the latest, when the
sample that showed it was scraped, which the job reads with `timestamp()` rather than taking the
query's step, less the seconds of the stages it had ended by then; each later sample of it, with more stages
ended, can only make that earlier (`Launch.began`). The node's collector starts with the instance, as
the agent does, so its first scrape may come well after the stages it shows had ended: only an
observation bounds when the launch began from below (`Launch.began_after`). A scrape of the launch
that lacks a stage it ends later says the stage had not ended then, so the launch began after that
scrape less the seconds of the stages up to that one, in the order a launch goes through them; and a
scrape of the instance's launch before says the new one had not begun then. A launch without its boot
stage, which the agent omits when it cannot read the uptime, counts its stages from an unknown time
after the kernel's start, so its own scrapes bound nothing from below. Where nothing bounds it,
`began_after_at` is null. A launch is written when these two bounds may overlap the hour, with
both: `in_hour` is `certain` when both are in it, and `uncertain` when the hour's start or end
falls between them or the earliest is unknown, as for a launch that began in the hour's last
seconds and was first scraped after it.
A launch shows no stage before its boot has ended, so one that began near the hour's end is seen
only after it: the job goes on reading for `DISCOVERY_SECONDS` after the hour, and a launch that
began in the hour keeps the stages it ends after it: the job waits for each to end its `hook`, the
last stage, for at most `LAUNCH_WAIT_SECONDS` after the hour.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-dry_run_class`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output: a host name, reached over HTTPS (`common.gateway_address`) |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_DRY_RUN_CLASS_SIZE` | The class size, in students: the sessions it replays. Required |
| `RHI_DRY_RUN_SHAPE` | `hands-on` or `several-agents`. Required |
| `RHI_SPIKE_INSTANCES` | The instances the rehearsal needs in service: `1` unless given |
| `RHI_SPIKE_RUN_MINUTES` | How long the job lasts, in minutes, which the session must outlast. Required |
| `RHI_SPIKE_POOL_STATE_TABLE` | The name of the stage's pool-state table, the pools stack's output |

The pool is the one named `coursework` (`POOL`), always; its engine comes from the deployed
configuration (`rhi.config.read_ssm`), and the region from CodeBuild's `AWS_REGION`. The job refuses,
before it sends anything, a class size that is missing or below one, a shape of another name, a
run whose minutes are missing or whose minutes or instances are not a whole number of one or more
(`no_run_needs`), a session that does not carry the run, and a class larger than the canary
students' keys.
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

# Seconds. Until the pool is ready its status is read this often, for as long as a cold launch
# may take at worst (spike S1).
POLL_SECONDS = 5.0
READY_WAIT_SECONDS = 40 * 60.0
# Seconds before the next conversation of an agent whose conversation ended refused or failed.
AFTER_REFUSAL_SECONDS = 10.0
# Seconds. The collectors scrape every 15 seconds; the samples of the hour's last answers reach the
# workspace within a few scrapes, and the job waits that long before its end readings.
SCRAPE_SECONDS = 15.0
SETTLE_SECONDS = 4 * SCRAPE_SECONDS
# Seconds the job waits, after the hour, for a launch that began in it to end its last stage: a cold
# launch at worst (spike S1).
LAUNCH_WAIT_SECONDS = READY_WAIT_SECONDS
# Seconds the job goes on reading the stages after the hour for a launch it has not seen yet: one that
# began before the hour's end shows its first stage once its boot has ended, which this leaves ample
# time for.
DISCOVERY_SECONDS = 15 * 60.0
# Seconds. Prometheus' lookback: how far back a reading at a time finds a series' last sample.
LOOKBACK_SECONDS = 300.0
# Seconds a series' last sample may be old and still be read as written now: four scrapes. The
# scaler's gauges are written once a minute, and may be two minutes old (`s7_drain.FRESH_SECONDS`).
WRITING_SECONDS = 4 * SCRAPE_SECONDS
SCALER_FRESH_SECONDS = 120.0
# Two stage samples of one launch give a stage the same seconds; a new launch gives it others.
_SAME_SECONDS = 1e-6

_STAGES = frozenset(stage.value for stage in Stage)
# The stages in the order a launch goes through them (`rhi_lifecycle.stages`), each ending where the
# next starts.
_ORDER = tuple(
    stage.value
    for stage in (
        Stage.BOOT,
        Stage.IMAGE,
        Stage.RELEASE_CHECK,
        Stage.CONTAINER_START,
        Stage.WEIGHTS,
        Stage.WARM_UP,
        Stage.HOOK,
    )
)
# The labels a launch's series may carry in the workspace. The agent gives one, a launch's
# (`Transition`); the agents before it gave two more, and a launch's label could move along them in
# this order: a launch into service was taken for a promotion before it turned out to be a cold
# launch. A step that shows two takes the later.
_TRANSITIONS = ("entry", "promotion", Transition.LAUNCH.value)
_LAST_STAGE = Stage.HOOK.value


class Gateway(Protocol):
    """What the job asks the gateway: `common.Gateway`, or a test's."""

    def state(self) -> str:
        """The pool's state as the status gives it; empty when it could not be read."""
        ...


def conversation_of(seed: int) -> loadgen.Session:
    """One conversation of an agent: section 4.7's sizes, drawn from `seed`, as Claude Code sends it."""
    return AsClaudeCode(loadgen.agent_session(POOL, TURNS_PER_SESSION, seed))


async def _off_loop[Result](action: Callable[[], Result]) -> Result:
    """Runs `action`, a blocking read, in a thread."""
    return await asyncio.to_thread(action)


# The stages of the launches.


type Snapshot = dict[str, float]


@dataclass
class Launch:
    """One launch of an instance, as the stage metric showed it: its last transition label, the step
    it was first seen at, the latest it can have begun, and the seconds of each stage it had ended by
    its last sample. The stages follow each other from the kernel's start, so at each sample the
    launch began no later than the sample's time less the seconds of the stages ended by then: the
    latest it can have begun is the earliest of these.

    `began_after` is what the observations say it began after, None when none says it: the last
    scrape of the instance's launch before, and each scrape of this one that lacked a stage it ended
    later, less the seconds of the stages up to that one (`launches_of`)."""

    instance: str
    transition: str
    first_seen: float
    began: float
    stages: Snapshot
    began_after: float | None = None


def _began_after(stages: Snapshot, floor: float | None, seen: Iterable[tuple[float, frozenset[str]]]) -> float | None:
    """The latest of the bounds the observations give from below a launch with `stages`, or None:
    `floor`, and for each scrape `seen` at a time with the stages it showed, its time less the seconds
    of the stages up to the first one it lacked. The stages count from the kernel's start only with
    the boot among them: the agent writes no boot when it cannot read the uptime, and then the time
    from the kernel's start to the first stage it measured is unknown, so a scrape of the launch
    bounds nothing from below, and only `floor` does."""
    bounds = [] if floor is None else [floor]
    if Stage.BOOT.value not in stages:
        return floor
    for at, ended in seen:
        through = 0.0
        for stage in _ORDER:
            if stage not in stages:
                continue
            through += stages[stage]
            if stage not in ended:
                bounds.append(at - through)
                break
    return max(bounds, default=None)


def _same_launch(earlier: Snapshot, later: Snapshot) -> bool:
    """Whether two snapshots of the stages can be of one launch: no stage of both has two numbers."""
    return all(abs(earlier[stage] - later[stage]) <= _SAME_SECONDS for stage in earlier.keys() & later.keys())


def _identity(labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((name, value) for name, value in labels.items() if name != "__name__"))


def launches_of(
    series: Iterable[common.Series], start: float, scraped: Iterable[common.Series] = ()
) -> list[Launch]:
    """The launches seen after `start` in the stage metric's `series`, read from before it.

    `scraped` is the same metric read through `timestamp()`: at each step of the range query, when
    the sample it returns was scraped. A range query returns, at each of its steps, the last sample
    scraped before it, so the step is only the latest the sample can have been taken at; each sample
    is placed at its scrape time, and at its step where `scraped` does not give one.

    Per instance, the stages scraped at one time form a snapshot, whatever their transition label.
    The last one at or before `start` is the launch from before the hour; each later snapshot that
    gives a stage other seconds than the launch it follows begins a new one, and one that agrees with
    it adds the stages it ended since and gives it its label. A launch began, at the latest, at the
    earliest of its samples' times less the seconds of the stages it had ended by each. A label
    outside the agent's fixed stages and transitions is not read."""
    scrapes = {
        (_identity(one.labels), at): taken
        for one in scraped
        for at, taken in one.samples
        if math.isfinite(taken)
    }
    # Per instance and time, the labels and stages seen then, and whether the time is a scrape's.
    steps: dict[str, dict[float, tuple[set[str], Snapshot, list[bool]]]] = {}
    for one in series:
        instance, transition, stage = (one.labels.get(label, "") for label in ("instance", "transition", "stage"))
        if not instance or transition not in _TRANSITIONS or stage not in _STAGES:
            continue
        identity = _identity(one.labels)
        for step, seconds in one.samples:
            if math.isfinite(seconds):
                taken = scrapes.get((identity, step))
                at = step if taken is None else min(step, taken)
                labels, snapshot, scraped_then = steps.setdefault(instance, {}).setdefault(at, (set(), {}, [False]))
                labels.add(transition)
                snapshot[stage] = seconds
                scraped_then[0] = scraped_then[0] or taken is not None
    # Each launch after `start`, with what bounds it from below: the last scrape of the launch before
    # it, and the scrapes of it with the stages each showed. A time that is only a step is no scrape's:
    # the sample it shows was scraped at it or before, so it bounds nothing from below.
    found: list[tuple[Launch, float | None, list[tuple[float, frozenset[str]]]]] = []
    for instance, snapshots in steps.items():
        current: Launch | None = None
        seen: list[tuple[float, frozenset[str]]] = []
        for at in sorted(snapshots):
            labels, snapshot, (scraped_then,) = snapshots[at]
            transition = max(labels, key=_TRANSITIONS.index)
            if current is not None and _same_launch(current.stages, snapshot):
                current.stages.update(snapshot)
                current.transition = transition
                current.began = min(current.began, at - sum(current.stages.values()))
                if scraped_then:
                    seen.append((at, frozenset(snapshot)))
                continue
            floor = max((time for time, _ in seen), default=None)
            current = Launch(instance, transition, at, at - sum(snapshot.values()), dict(snapshot))
            seen = [(at, frozenset(snapshot))] if scraped_then else []
            if at > start:
                found.append((current, floor, seen))
    for launch, floor, seen in found:
        launch.began_after = _began_after(launch.stages, floor, seen)
    return sorted((launch for launch, _, _ in found), key=lambda launch: launch.first_seen)


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
        """Waits until the gateway says the pool is ready. TimeoutError when it is not within
        `READY_WAIT_SECONDS`."""
        self.step = "ready"
        started = self.clock.now()
        while (await self.offload(self.gateway.state)) != State.READY:
            if self.clock.now() - started >= READY_WAIT_SECONDS:
                raise TimeoutError("the pool was not ready in time")
            await self.clock.sleep(POLL_SECONDS)
        self.recorder.line("pool_ready", waited_seconds=round(self.clock.now() - started, 3))

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
        """Each instance's counters of the engine's row at `at`, as they stood: one line. A counter
        that could not be read is an `error` line, and is named in `unread`; the others are kept."""
        instances: dict[str, dict[str, float]] = {}
        unread: list[str] = []
        for quantity, name in self._counters().items():
            matchers = [f'pool="{POOL}"', *(f'{label}="{value}"' for label, value in name.labels.items())]
            query = f"sum by (instance) ({name}_total{{{','.join(matchers)}}})"
            try:
                found = await self._per_instance(query, at)
            except Exception as error:  # noqa: BLE001 - recorded by its kind: no text of AWS's reaches the log
                unread.append(quantity)
                self.recorder.line("error", step=f"{moment}_counters", quantity=quantity, error=error_kind(error).value)
                continue
            for instance, value in found.items():
                instances.setdefault(instance, {})[quantity] = value
        self.recorder.line("engine_counters", moment=moment, read_at=utc(at), instances=instances, unread=unread)

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

    async def _launches(self, start: float, deadline: float) -> list[Launch]:
        """The launches that may have begun between `start` and `deadline`, with every stage they ended
        up to now: those whose bounds, `began_after` to `began`, may overlap the hour; one whose earliest
        is unknown may have begun at any time before its latest."""
        until = max(deadline, self.clock.now())
        selector = f'{STAGE_METRIC}{{pool="{POOL}"}}'
        series = await self._read(selector, start - LOOKBACK_SECONDS, until)
        scraped = await self._read(f"timestamp({selector})", start - LOOKBACK_SECONDS, until)
        launches = launches_of(series, start, scraped)
        return [
            launch
            for launch in launches
            if start < launch.began and (launch.began_after is None or launch.began_after < deadline)
        ]

    async def started_instances(self, start: float, deadline: float, *, wait: float = 0.0) -> None:
        """A line for each launch that may have begun between `start` and `deadline`, from the stage metric
        (`launches_of`). It reads until `DISCOVERY_SECONDS` after `deadline`, so that a launch still in
        its boot then is seen, and until each launch has ended its last stage; never longer than
        `wait` seconds from now."""
        waited_from = self.clock.now()
        launches = await self._launches(start, deadline)
        while (
            self.clock.now() < deadline + DISCOVERY_SECONDS
            or any(_LAST_STAGE not in launch.stages for launch in launches)
        ):
            if self.clock.now() - waited_from >= wait:
                break
            await self.clock.sleep(SCRAPE_SECONDS)
            launches = await self._launches(start, deadline)
        for launch in launches:
            after = launch.began_after
            self.recorder.line(
                "instance_started",
                instance=self.names(launch.instance),
                transition=launch.transition,
                began_at=utc(launch.began),
                began_after_at=None if after is None else utc(after),
                in_hour="certain" if after is not None and start <= after and launch.began <= deadline else "uncertain",
                first_seen_at=utc(launch.first_seen),
                stages=launch.stages,
            )

    async def _guarded(self, step: str, reading: Awaitable[None]) -> None:
        """Makes one reading; one that fails is written as an `error` line, and the run goes on."""
        try:
            await reading
        except Exception as error:  # noqa: BLE001 - recorded by its kind: no text of AWS's reaches the log
            self.recorder.line("error", step=step, error=error_kind(error).value)

    # The class.

    def _request_line(self, session: int, agent: int, conversation: int, record: loadgen.Record) -> None:
        sent = record.sent_at if record.sent_at is not None else self.clock.now()

        def at(seconds: float | None) -> str | None:
            return None if seconds is None else utc(sent + seconds)

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
            held=record.held,
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
            last = records[-1] if records else None
            if last is not None and (
                last.failure is not None or last.cause is not None or last.status != httpx.codes.OK
            ):
                await self.clock.sleep(AFTER_REFUSAL_SECONDS)

    async def replay(self, deadline: float) -> None:
        """The class until `deadline`: every agent of every session, to the end of its last request."""
        async with asyncio.TaskGroup() as agents:
            for session in range(self.class_size):
                for agent in range(self.agents):
                    agents.create_task(self._agent(session, agent, deadline))

    async def _start_readings(self, start: float) -> None:
        await self._guarded("start_counters", self.engine_counters("start", start))
        await self._guarded("cache_capacity", self.cache_capacity(start))
        await self._guarded("start_in_service", self.in_service("start", start))

    async def run(self) -> None:
        """The pool made ready; the hour, while the start readings are made of its first moment; then
        the end readings, of its last moment, and the launches that began in it."""
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
        self.step = "replay"
        start = self.clock.now()
        deadline = start + self.duration
        self.recorder.line("hour_started", started_at=utc(start), ends_at=utc(deadline))
        async with asyncio.TaskGroup() as group:
            group.create_task(self._start_readings(start))
            group.create_task(self.replay(deadline))
        self.recorder.line("hour_ended", ended_at=utc(self.clock.now()), requests=self.requests)
        self.step = "settle"
        await self.clock.sleep(SETTLE_SECONDS)
        self.step = "end_readings"
        await self._guarded("end_counters", self.engine_counters("end", deadline))
        await self._guarded("end_in_service", self.in_service("end", deadline))
        launches = self.started_instances(start, deadline, wait=LAUNCH_WAIT_SECONDS)
        await self._guarded("started_instances", launches)
        self.recorder.line("end", requests=self.requests)


def class_size_of(environ: Mapping[str, str]) -> int | None:
    """The class size the owner gave, or None when it is missing or not a whole number of one or more."""
    given = environ.get(CLASS_SIZE_VARIABLE, "").strip()
    # ASCII digits only: `int` refuses some characters `isdigit` takes, such as a superscript.
    return int(given) if given.isascii() and given.isdigit() and int(given) >= 1 else None


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
    step = "configuration"
    rehearsal: Rehearsal | None = None
    try:
        class_size, shape = class_size_of(environ), shape_of(environ)
        if class_size is None or shape is None:
            recorder.line("refused", reason="no_class_size" if class_size is None else "no_shape")
            return 1
        try:
            needs = common.needs_of(environ)
        except ValueError:
            recorder.line("refused", reason="no_run_needs")
            return 1
        settings = config.read_ssm()
        pools = [pool for pool in settings.pools if pool.name == POOL]
        if len(pools) != 1:
            recorder.line("refused", reason="no_coursework_pool")
            return 1
        endpoint = common.gateway_address(environ[ENDPOINT_VARIABLE])
        step = "session"
        try:
            common.require_session(
                common.pool_state_table(environ),
                POOL,
                instances=needs.instances,
                run_minutes=needs.run_minutes,
                session_max_hours=settings.session_max_hours,
                now=clock.now,
            )
        except common.SessionRefused as refused:
            recorder.line("refused", reason=refused.reason.value)
            return 1
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
            gateway=common.Gateway(endpoint, POOL, keys[0]),
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
