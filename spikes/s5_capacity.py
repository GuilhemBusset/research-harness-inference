"""Spike S5, the runs: replayed agent sessions against a pool, and what each run measured (system
design, sections 0, 4.7, 7.2, 12 and 17).

What one instance serves is an estimate until it is measured (section 4.7). This job takes the
measurements: it replays agent sessions of both shapes against a pool, and reads what each run
measured, from the engine through the workspace and from the students' side through the load
generator's records. The verdict against the floors, the record and the posting are not this job's:
it prints each run's numbers, one JSON line a run, for them.

**Nothing in it is the coursework pool's own.** The pool, the stream counts, the class size, the
duration and the lever are inputs, so the upgrade pool's spike S6 runs the same job with its own.

**The runs** (`plan`). For each shape of section 4.7, `hands-on` and `several-agents`, one run at
each number of streams the job is given, then one at the class size it is given:

- at a number of streams, that many agent sessions run at once: hands-on, one student each; several
  agents, two sessions a student (`loadgen.AGENTS_PER_STUDENT`), so half as many students, and an
  odd count leaves the last student one session;
- at the class size, every student of the class runs at once in the shape: hands-on, one session
  each, as many streams as students; several agents, two each, twice as many streams.

Each student is a canary student of their own, with their own key (`common.canary_keys`), as the
load generator sends them (`loadgen.replay`): a student's sessions share the student's key, cache
salt and limits, as a real student's agents do, and every session is its own in the records.

**One run** first wakes the pool through the gateway and waits until it is ready, so that a run
measures a warm pool, never a cold start. It then replays its sessions (`loadgen.agent_session`, of
section 4.7's sizes) for the duration it is given: no session sends a request once the duration
has passed (`Until`), and the run ends when the requests in flight have. A session holds
`TURNS_PER_SESSION` turns, which keeps its transcript inside the table's longest row; a session
that ends before the duration is followed, once every session of its round has ended, by a new
round of sessions, whose first turns are first turns again: new text, so nothing of it is cached.

**What the engine measured**, per instance, read from the workspace over the run's window by the
names of the per-engine table (`rhi_common.engine_metrics`), as section 7.2 defines each:

| Quantity | How |
| --- | --- |
| Output speed per stream | The mean: output tokens of the requests over their decode time. The median and the slowest tenth: the inverse of the time per output token at its 50th and 90th percentiles |
| Output throughput | Output tokens over the window's seconds |
| Prefill speed | Tokens computed in prefill over prefill time |
| Time to first token | The engine's histogram at the median and the 95th percentile, with the requests behind it |
| Cached share of input | Cached input tokens over input tokens |
| Cache use | The share in use, its mean and its peak |
| Preemptions | Their count in the window |
| Running and waiting requests | Each one's mean and peak |
| Input and output tokens per request | Each distribution's requests, by bucket |

A quantity an engine's row leaves out (the mock's has none of the further ones) reads `None`, not
zero, as does one with nothing to divide. Counters and histograms are increased per series before
they are summed per instance: an instance's counters restart from zero with its engine.

**What each instance's lifecycle agent reported**: the cache capacity in tokens the engine gave at
start (`rhi_lifecycle.cache.METRIC`), and its release (`rhi_lifecycle.serving.RELEASE_METRIC`),
which an instance writes only while in service: the build, its `model_revision`, and from the
times of its samples, the seconds the instance was in service during the window. A pool whose
instances report two builds in one window is refused: the run is no one build's.

**What the students got**, from the load generator's records (`experience_of`): the time to first
token at the median and the 95th percentile, for later turns and for first turns apart; the speed
per stream at the median and the slowest tenth; and the cost per million output tokens
(`cost_per_million`): each instance's seconds in service at the configured price of its instance
type, over the output tokens the records report.

**The label** of each run: the pool, the instance types that ran, the build, the lever, the shape,
the number of streams, the students, and the class size when the run is at it.

**What the job is given**, in its environment, as the owner starts it (`rhi-spike-s5_capacity`):

| Variable | What it is |
| --- | --- |
| `RHI_SPIKE_ENDPOINT` | The gateway's address, the gateway stack's output: a host name, or an `https://` address |
| `RHI_SPIKE_WORKSPACE_URL` | The Prometheus workspace's query address, the observability stack's output |
| `RHI_SPIKE_POOL` | The pool, `coursework` unless given |
| `RHI_S5_STREAMS` | The numbers of streams, separated by commas: `1,4,8,16` unless given |
| `RHI_S5_CLASS_SIZE` | The class's size, in students |
| `RHI_S5_DURATION_SECONDS` | How long each run sends requests, in seconds |
| `RHI_S5_LEVER` | The name of the lever the pool is deployed with, lower-case letters, digits and hyphens: `baseline` unless given |

The pool's engine and prices come from the deployed configuration (`rhi.config.read_ssm`), the
region from CodeBuild's `AWS_REGION`. Each lever is a configuration the owner deploys before
starting the job; the job never changes the pool. Run it while nothing else sends to the pool.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

import boto3

import common
import loadgen
import s4_weights
from rhi import config
from rhi_common.engine_metrics import Further, Kind, Name, Quantity, further_names, metric_names
from rhi_lifecycle.cache import METRIC as CACHE_CAPACITY_METRIC
from rhi_lifecycle.serving import RELEASE_METRIC

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence

JOB = "s5_capacity"

ENDPOINT_VARIABLE = "RHI_SPIKE_ENDPOINT"
WORKSPACE_URL_VARIABLE = "RHI_SPIKE_WORKSPACE_URL"
POOL_VARIABLE = "RHI_SPIKE_POOL"
STREAMS_VARIABLE = "RHI_S5_STREAMS"
CLASS_SIZE_VARIABLE = "RHI_S5_CLASS_SIZE"
DURATION_VARIABLE = "RHI_S5_DURATION_SECONDS"
LEVER_VARIABLE = "RHI_S5_LEVER"
DEFAULT_POOL = "coursework"
DEFAULT_STREAMS = (1, 4, 8, 16)
DEFAULT_LEVER = "baseline"

# Turns after a session's first. A transcript starts at up to 100,000 tokens and a turn adds up to
# 5,000 and the answer before it: ten keep it inside section 4.7's longest row, 150,000.
TURNS_PER_SESSION = 10
# Seconds. The collector scrapes every 15 seconds; the samples of a run's end reach the workspace
# within a few scrapes, and the job waits that long before it reads.
SCRAPE_SECONDS = 15
SETTLE_SECONDS = 4 * SCRAPE_SECONDS
# Seconds. While the pool wakes: its status is read this often, and the wake repeated this often,
# as `rhi wait` does; a cold launch may take 40 minutes at worst (spike S1).
POLL_SECONDS = 5
WAKE_EVERY_SECONDS = 60
READY_WAIT_SECONDS = 40 * 60
READY = "ready"

_LEVER = re.compile(r"[a-z0-9-]+")


class Shape(StrEnum):
    """The two shapes of section 4.7."""

    HANDS_ON = "hands-on"
    SEVERAL_AGENTS = "several-agents"


# The runs.


@dataclass(frozen=True)
class Planned:
    """One run to make: its shape, the sessions it runs at once, the students they are spread over,
    and the class size when it is the run at the class's size."""

    shape: Shape
    streams: int
    students: int
    class_size: int | None = None


def plan(streams: Sequence[int], class_size: int) -> list[Planned]:
    """The runs, for each shape: one at each number of streams, then one at the class size.
    ValueError for no number of streams, or a count below one."""
    if not streams or any(count < 1 for count in (*streams, class_size)):
        raise ValueError("spike S5 runs one stream or more, and a class of one student or more")
    per = loadgen.AGENTS_PER_STUDENT
    runs = []
    for shape in Shape:
        for count in streams:
            students = count if shape is Shape.HANDS_ON else -(-count // per)
            runs.append(Planned(shape, count, students))
        sessions = class_size if shape is Shape.HANDS_ON else class_size * per
        runs.append(Planned(shape, sessions, class_size, class_size))
    return runs


def load_of(planned: Planned, model: str, seed: int) -> loadgen.Load:
    """The sessions of one round of `planned`, for the pool `model` names, drawn from `seed` on."""
    if planned.shape is Shape.HANDS_ON:
        return loadgen.hands_on(model, planned.students, turns=TURNS_PER_SESSION, seed=seed)
    load = loadgen.several_agents(model, planned.students, turns=TURNS_PER_SESSION, seed=seed)
    return loadgen.Load(load.sessions[: planned.streams], sessions_per_student=load.sessions_per_student)


@dataclass(frozen=True)
class Until:
    """A session that sends no request once `deadline`, on `clock`, has passed: none whose pause
    would end at the deadline or after it."""

    session: loadgen.Session
    deadline: float
    clock: Callable[[], float]

    def requests(self) -> Generator[tuple[float, loadgen.Request], str]:
        steps = self.session.requests()
        step = next(steps, None)
        while step is not None and self.clock() + step[0] < self.deadline:
            answer = yield step
            try:
                step = steps.send(answer)
            except StopIteration:
                return


# What the students got.


@dataclass(frozen=True)
class Experience:
    """What the records of a run say each request got. A number with no sample behind it is None."""

    requests: int
    failed: int
    later_turns: int
    first_turns: int
    first_token_seconds_median: float | None
    first_token_seconds_p95: float | None
    first_turn_first_token_seconds_median: float | None
    first_turn_first_token_seconds_p95: float | None
    tokens_per_second_median: float | None
    tokens_per_second_slowest_tenth: float | None
    output_tokens: int


def _at(samples: Sequence[float], percent: float) -> float | None:
    return common.percentile(samples, percent) if samples else None


def experience_of(records: Sequence[loadgen.Record]) -> Experience:
    """The time to first token at the median and the 95th percentile, later turns and first turns
    apart; the speed per stream at the median and the slowest tenth, its 10th percentile; the
    output tokens the answers reported."""
    later = [r.first_token_seconds for r in records if not r.first_turn and r.first_token_seconds is not None]
    first = [r.first_token_seconds for r in records if r.first_turn and r.first_token_seconds is not None]
    speeds = [r.output_tokens_per_second for r in records if r.output_tokens_per_second is not None]
    return Experience(
        requests=len(records),
        failed=sum(record.status != 200 for record in records),
        later_turns=len(later),
        first_turns=len(first),
        first_token_seconds_median=_at(later, 50),
        first_token_seconds_p95=_at(later, 95),
        first_turn_first_token_seconds_median=_at(first, 50),
        first_turn_first_token_seconds_p95=_at(first, 95),
        tokens_per_second_median=_at(speeds, 50),
        tokens_per_second_slowest_tenth=_at(speeds, 10),
        output_tokens=sum(record.output_tokens or 0 for record in records),
    )


# What the lifecycle agents reported.


@dataclass(frozen=True)
class InService:
    """An instance's release in the window: its instance type, its build, and its seconds in service."""

    instance_type: str
    build: str
    seconds: float


def release_query(pool: str) -> str:
    """The times of the pool's release samples, with their labels: a range query over the window."""
    return f'timestamp({RELEASE_METRIC}{{pool="{pool}"}})'


def in_service_of(release: Sequence[common.Series], start: float, end: float) -> dict[str, InService]:
    """Each instance's release from `start` to `end`, from the times of its release samples
    (`release_query`): in service from its first sample, or the window's start, to a scrape after
    its last, or the window's end. ValueError for an instance whose samples carry no instance type
    or two releases."""
    times: dict[str, list[float]] = {}
    kinds: dict[str, set[tuple[str, str]]] = {}
    for one in release:
        instance = one.labels.get("instance", "")
        if not instance or not one.samples:
            continue
        times.setdefault(instance, []).extend(one.values())
        kinds.setdefault(instance, set()).add(
            (one.labels.get("instance_type", ""), one.labels.get("model_revision", ""))
        )
    found = {}
    for instance, taken in times.items():
        if len(kinds[instance]) != 1 or not all(next(iter(kinds[instance]))):
            raise ValueError("an instance reported no instance type, no build, or two of either in the window")
        ((kind, build),) = kinds[instance]
        seconds = max(0.0, min(end, max(taken) + SCRAPE_SECONDS) - max(start, min(taken)))
        found[instance] = InService(kind, build, seconds)
    return found


def build_of(in_service: Mapping[str, InService]) -> str:
    """The one build the pool's instances served. ValueError for none, or two."""
    builds = {one.build for one in in_service.values()}
    if len(builds) != 1:
        raise ValueError(f"the pool's instances reported {len(builds)} builds in the run, not one")
    (build,) = builds
    return build


def cost_per_million(
    in_service: Mapping[str, InService], prices: Mapping[str, float], output_tokens: int
) -> float | None:
    """Dollars per million output tokens: each instance's seconds in service at the configured
    hourly price of its type, over `output_tokens`. None for no token. ValueError for an instance of
    a type with no price."""
    if any(one.instance_type not in prices for one in in_service.values()):
        raise ValueError("an instance of a type the pool does not price ran")
    if output_tokens <= 0:
        return None
    dollars = sum(prices[one.instance_type] * one.seconds / 3600 for one in in_service.values())
    return dollars / output_tokens * 1_000_000


# What the engine measured.


class Key(StrEnum):
    """One query of the engine's quantities, each answered per instance."""

    OUTPUT_TOKENS = "output_tokens"
    INPUT_TOKENS = "input_tokens"
    CACHED_INPUT_TOKENS = "cached_input_tokens"
    FIRST_TOKEN_REQUESTS = "first_token_requests"
    FIRST_TOKEN_MEDIAN = "first_token_median"
    FIRST_TOKEN_P95 = "first_token_p95"
    TOKEN_TIME_MEDIAN = "token_time_median"
    TOKEN_TIME_P90 = "token_time_p90"
    RUNNING_MEAN = "running_mean"
    RUNNING_PEAK = "running_peak"
    WAITING_MEAN = "waiting_mean"
    WAITING_PEAK = "waiting_peak"
    CACHE_USE_MEAN = "cache_use_mean"
    CACHE_USE_PEAK = "cache_use_peak"
    CACHE_CAPACITY = "cache_capacity"
    REQUEST_OUTPUT_TOKENS = "request_output_tokens"
    DECODE_SECONDS = "decode_seconds"
    PREFILL_COMPUTED_TOKENS = "prefill_computed_tokens"
    PREFILL_SECONDS = "prefill_seconds"
    PREEMPTIONS = "preemptions"
    INPUT_DISTRIBUTION = "input_distribution"
    OUTPUT_DISTRIBUTION = "output_distribution"


def _selector(name: Name, suffix: str, pool: str) -> str:
    matchers = [f'pool="{pool}"', *(f'{label}="{value}"' for label, value in name.labels.items())]
    return f"{name}{suffix}{{{','.join(matchers)}}}"


def queries(pool: str, engine: str, window_seconds: float) -> dict[Key, str]:
    """The queries of the engine's quantities on the pool, over the window that ends where they are
    evaluated, by the names of `engine`'s row; a further quantity the row leaves out has none."""
    names, further = metric_names(engine), further_names(engine)
    window = f"{max(1, math.ceil(window_seconds))}s"

    def increase(name: Name, suffix: str = "", by: str = "instance") -> str:
        if not suffix:
            suffix = "_total" if name.kind is Kind.COUNTER else ""
        return f"sum by ({by}) (increase({_selector(name, suffix, pool)}[{window}]))"

    def quantile(fraction: float, name: Name) -> str:
        return f"histogram_quantile({fraction}, {increase(name, '_bucket', 'instance, le')})"

    def over_time(function: str, aggregate: str, name: Name) -> str:
        return f"{function}({aggregate} by (instance) ({_selector(name, '', pool)})[{window}:{SCRAPE_SECONDS}s])"

    ttft, tpot = names[Quantity.TIME_TO_FIRST_TOKEN], names[Quantity.TIME_PER_OUTPUT_TOKEN]
    found = {
        Key.OUTPUT_TOKENS: increase(names[Quantity.OUTPUT_TOKENS]),
        Key.INPUT_TOKENS: increase(names[Quantity.INPUT_TOKENS]),
        Key.CACHED_INPUT_TOKENS: increase(names[Quantity.CACHED_INPUT_TOKENS]),
        Key.FIRST_TOKEN_REQUESTS: increase(ttft, "_count"),
        Key.FIRST_TOKEN_MEDIAN: quantile(0.5, ttft),
        Key.FIRST_TOKEN_P95: quantile(0.95, ttft),
        Key.TOKEN_TIME_MEDIAN: quantile(0.5, tpot),
        # The slowest tenth of the streams' speed is the longest tenth of the time per token.
        Key.TOKEN_TIME_P90: quantile(0.9, tpot),
        Key.RUNNING_MEAN: over_time("avg_over_time", "sum", names[Quantity.RUNNING_REQUESTS]),
        Key.RUNNING_PEAK: over_time("max_over_time", "sum", names[Quantity.RUNNING_REQUESTS]),
        Key.WAITING_MEAN: over_time("avg_over_time", "sum", names[Quantity.WAITING_REQUESTS]),
        Key.WAITING_PEAK: over_time("max_over_time", "sum", names[Quantity.WAITING_REQUESTS]),
        Key.CACHE_USE_MEAN: over_time("avg_over_time", "max", names[Quantity.CACHE_USAGE]),
        Key.CACHE_USE_PEAK: over_time("max_over_time", "max", names[Quantity.CACHE_USAGE]),
        Key.CACHE_CAPACITY: f'max by (instance) (last_over_time({CACHE_CAPACITY_METRIC}{{pool="{pool}"}}[{window}]))',
    }
    sums = {
        Key.REQUEST_OUTPUT_TOKENS: Further.REQUEST_OUTPUT_TOKENS,
        Key.DECODE_SECONDS: Further.DECODE_TIME,
        Key.PREFILL_COMPUTED_TOKENS: Further.REQUEST_PREFILL_COMPUTED_TOKENS,
        Key.PREFILL_SECONDS: Further.PREFILL_TIME,
    }
    found |= {key: increase(further[quantity], "_sum") for key, quantity in sums.items() if quantity in further}
    if Further.PREEMPTIONS in further:
        found[Key.PREEMPTIONS] = increase(further[Further.PREEMPTIONS])
    distributions = {
        Key.INPUT_DISTRIBUTION: Further.REQUEST_INPUT_TOKENS,
        Key.OUTPUT_DISTRIBUTION: Further.REQUEST_OUTPUT_TOKENS,
    }
    found |= {
        key: increase(further[quantity], "_bucket", "instance, le")
        for key, quantity in distributions.items()
        if quantity in further
    }
    return found


@dataclass(frozen=True)
class EngineQuantities:
    """What one instance's engine measured in a run. None where the engine's row has no name for
    it, the workspace gave nothing, or there was nothing to divide."""

    output_tokens_per_second_per_stream_mean: float | None = None
    output_tokens_per_second_per_stream_median: float | None = None
    output_tokens_per_second_per_stream_slowest_tenth: float | None = None
    output_tokens_per_second: float | None = None
    prefill_tokens_per_second: float | None = None
    first_token_requests: float | None = None
    first_token_seconds_median: float | None = None
    first_token_seconds_p95: float | None = None
    cached_share: float | None = None
    cache_use_mean: float | None = None
    cache_use_peak: float | None = None
    cache_capacity_tokens: float | None = None
    preemptions: float | None = None
    running_requests_mean: float | None = None
    running_requests_peak: float | None = None
    waiting_requests_mean: float | None = None
    waiting_requests_peak: float | None = None
    input_tokens_per_request: dict[str, float] | None = None
    """Requests by the upper bound of their bucket, Prometheus' `le`."""
    output_tokens_per_request: dict[str, float] | None = None


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _ratio(above: float | None, below: float | None) -> float | None:
    return None if above is None or not below else _finite(above / below)


def quantities_of(answers: Mapping[Key, Sequence[common.Series]], window_seconds: float) -> dict[str, EngineQuantities]:
    """Each instance's quantities, from the workspace's answers to `queries`, by instance."""
    scalars: dict[str, dict[Key, float | None]] = {}
    buckets: dict[str, dict[Key, dict[str, float]]] = {}
    for key, series in answers.items():
        for one in series:
            instance = one.labels.get("instance", "")
            if not instance or not one.samples:
                continue
            value = one.samples[-1][1]
            if key in (Key.INPUT_DISTRIBUTION, Key.OUTPUT_DISTRIBUTION):
                buckets.setdefault(instance, {}).setdefault(key, {})[one.labels.get("le", "")] = value
            else:
                scalars.setdefault(instance, {})[key] = _finite(value)
    found = {}
    for instance in sorted({*scalars, *buckets}):
        held, counts = scalars.get(instance, {}), buckets.get(instance, {})

        def at(key: Key, held: Mapping[Key, float | None] = held) -> float | None:
            return held.get(key)

        output = at(Key.OUTPUT_TOKENS)
        found[instance] = EngineQuantities(
            output_tokens_per_second_per_stream_mean=_ratio(at(Key.REQUEST_OUTPUT_TOKENS), at(Key.DECODE_SECONDS)),
            output_tokens_per_second_per_stream_median=_ratio(1.0, at(Key.TOKEN_TIME_MEDIAN)),
            output_tokens_per_second_per_stream_slowest_tenth=_ratio(1.0, at(Key.TOKEN_TIME_P90)),
            output_tokens_per_second=None if output is None else output / window_seconds,
            prefill_tokens_per_second=_ratio(at(Key.PREFILL_COMPUTED_TOKENS), at(Key.PREFILL_SECONDS)),
            first_token_requests=at(Key.FIRST_TOKEN_REQUESTS),
            first_token_seconds_median=at(Key.FIRST_TOKEN_MEDIAN),
            first_token_seconds_p95=at(Key.FIRST_TOKEN_P95),
            cached_share=_ratio(at(Key.CACHED_INPUT_TOKENS), at(Key.INPUT_TOKENS)),
            cache_use_mean=at(Key.CACHE_USE_MEAN),
            cache_use_peak=at(Key.CACHE_USE_PEAK),
            cache_capacity_tokens=at(Key.CACHE_CAPACITY),
            preemptions=at(Key.PREEMPTIONS),
            running_requests_mean=at(Key.RUNNING_MEAN),
            running_requests_peak=at(Key.RUNNING_PEAK),
            waiting_requests_mean=at(Key.WAITING_MEAN),
            waiting_requests_peak=at(Key.WAITING_PEAK),
            input_tokens_per_request=counts.get(Key.INPUT_DISTRIBUTION),
            output_tokens_per_request=counts.get(Key.OUTPUT_DISTRIBUTION),
        )
    return found


# One run, and the job.


@dataclass(frozen=True)
class Label:
    """What a run is: the pool, the instance types that ran, the build, the lever, the shape, the
    sessions at once, the students, and the class size when the run is at it."""

    pool: str
    instance_types: tuple[str, ...]
    build: str
    lever: str
    shape: Shape
    streams: int
    students: int
    class_size: int | None


@dataclass(frozen=True)
class Instance:
    """One instance of a run: what its lifecycle agent reported, and what its engine measured."""

    instance_type: str | None
    seconds_in_service: float
    engine: EngineQuantities


@dataclass(frozen=True)
class Measured:
    """What one run measured."""

    label: Label
    seconds: float
    experience: Experience
    cost_per_million_output_tokens: float | None
    instances: dict[str, Instance] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class Reader(Protocol):
    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]: ...


class Replay(Protocol):
    def __call__(
        self, load: loadgen.Load, keys: Sequence[str], endpoint: str, *, at_once: int
    ) -> Awaitable[list[loadgen.Record]]: ...


class Job:
    """The runs of `plan(streams, class_size)` against the pool `pool`, served by `engine` at
    `prices`, each `duration` seconds, labelled with `lever`: sent through the gateway at
    `endpoint` as the canary students of `keys`, the pool woken through `gateway`, read from
    `workspace`."""

    def __init__(
        self,
        *,
        pool: str,
        engine: str,
        prices: Mapping[str, float],
        lever: str,
        streams: Sequence[int],
        class_size: int,
        duration: float,
        keys: Sequence[str],
        endpoint: str,
        gateway: Any,
        workspace: Reader,
        replay: Replay = loadgen.replay,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        if duration <= 0:
            raise ValueError("each run of spike S5 lasts some seconds")
        self.runs = plan(streams, class_size)
        if max(run.students for run in self.runs) > len(keys):
            raise ValueError("the runs need more canary students than there are keys")
        metric_names(engine)
        self.pool, self.engine, self.prices, self.lever = pool, engine, dict(prices), lever
        self.duration, self.keys, self.endpoint = duration, tuple(keys), endpoint
        self.gateway, self.workspace, self.replay = gateway, workspace, replay
        self.wall, self.sleep, self.log = wall, sleep, log
        self._seed = 0

    def _ready(self) -> None:
        started = woke = self.wall()
        if self.gateway.state() == READY:
            return
        self.gateway.wake()
        while self.gateway.state() != READY:
            if self.wall() - started >= READY_WAIT_SECONDS:
                raise TimeoutError("the pool was not ready in time")
            if self.wall() - woke >= WAKE_EVERY_SECONDS:
                self.gateway.wake()
                woke = self.wall()
            self.sleep(POLL_SECONDS)

    def _replay(self, planned: Planned, deadline: float) -> list[loadgen.Record]:
        records: list[loadgen.Record] = []
        while self.wall() < deadline:
            load = load_of(planned, self.pool, self._seed)
            self._seed += len(load.sessions)
            bounded = loadgen.Load(
                tuple(Until(session, deadline, self.wall) for session in load.sessions), load.sessions_per_student
            )
            round_records = asyncio.run(self._send(bounded))
            if not round_records:
                break
            records += round_records
        return records

    async def _send(self, load: loadgen.Load) -> list[loadgen.Record]:
        return await self.replay(load, self.keys, self.endpoint, at_once=len(load.sessions))

    def run(self, planned: Planned) -> Measured:
        """One run: the pool ready, the sessions replayed for the duration, then what it measured."""
        self._ready()
        start = self.wall()
        records = self._replay(planned, start + self.duration)
        end = self.wall()
        self.sleep(SETTLE_SECONDS)
        window = max(end - start, 1.0)
        release = self.workspace.read_series(release_query(self.pool), start=start, end=end, step=SCRAPE_SECONDS)
        in_service = in_service_of(release, start, end)
        build = build_of(in_service)
        answers = {
            key: self.workspace.read_series(query, start=end, end=end, step=SCRAPE_SECONDS)
            for key, query in queries(self.pool, self.engine, window).items()
        }
        engine = quantities_of(answers, window)
        experience = experience_of(records)
        instances = {
            instance: Instance(
                instance_type=in_service[instance].instance_type if instance in in_service else None,
                seconds_in_service=in_service[instance].seconds if instance in in_service else 0.0,
                engine=engine.get(instance, EngineQuantities()),
            )
            for instance in sorted({*in_service, *engine})
        }
        label = Label(
            pool=self.pool,
            instance_types=tuple(sorted({one.instance_type for one in in_service.values()})),
            build=build,
            lever=self.lever,
            shape=planned.shape,
            streams=planned.streams,
            students=planned.students,
            class_size=planned.class_size,
        )
        return Measured(
            label=label,
            seconds=window,
            experience=experience,
            cost_per_million_output_tokens=cost_per_million(in_service, self.prices, experience.output_tokens),
            instances=instances,
        )

    def measure(self) -> list[Measured]:
        """Every run, in the plan's order, each printed as one JSON line once measured."""
        measured = []
        for planned in self.runs:
            one = self.run(planned)
            self.log(one.to_json())
            measured.append(one)
        return measured


# The job's inputs.


def streams_of(environ: Mapping[str, str]) -> tuple[int, ...]:
    """The numbers of streams, `DEFAULT_STREAMS` unless given. ValueError for a count that is not a
    whole number of one or more, or one given twice."""
    written = environ.get(STREAMS_VARIABLE, "")
    counts = tuple(int(one) for one in written.split(",")) if written.strip() else DEFAULT_STREAMS
    if any(count < 1 for count in counts) or len(set(counts)) != len(counts):
        raise ValueError("the numbers of streams are distinct whole numbers of one or more")
    return counts


def whole_of(environ: Mapping[str, str], variable: str) -> int:
    """A whole number of one or more the job is given. ValueError for any other."""
    number = int(environ.get(variable, ""))
    if number < 1:
        raise ValueError(f"{variable} is a whole number of one or more")
    return number


def lever_of(environ: Mapping[str, str]) -> str:
    """The lever's name, `DEFAULT_LEVER` unless given. ValueError for another shape."""
    lever = environ.get(LEVER_VARIABLE, DEFAULT_LEVER)
    if not _LEVER.fullmatch(lever):
        raise ValueError("the lever's name is lower-case letters, digits and hyphens")
    return lever


def main(environ: Mapping[str, str] = os.environ) -> list[Measured]:
    """Runs every run with the job's environment and credentials, and prints what each measured."""
    streams, lever = streams_of(environ), lever_of(environ)
    class_size, duration = whole_of(environ, CLASS_SIZE_VARIABLE), whole_of(environ, DURATION_VARIABLE)
    # Refused before anything is sent: the canary keys go over HTTPS only.
    endpoint = s4_weights.gateway_address(environ.get(ENDPOINT_VARIABLE, ""))
    name = environ.get(POOL_VARIABLE, DEFAULT_POOL)
    pools = [pool for pool in config.read_ssm().pools if pool.name == name]
    if len(pools) != 1:
        raise ValueError("the deployed configuration has no pool of that name")
    (pool,) = pools
    session = boto3.Session()
    workspace = common.Workspace(environ[WORKSPACE_URL_VARIABLE], environ["AWS_REGION"], session.get_credentials())
    keys = common.canary_keys().students
    print(f"pool {name}, lever {lever}, streams {list(streams)}, class of {class_size}, {duration} s a run")
    return Job(
        pool=name,
        engine=pool.engine,
        prices=pool.prices,
        lever=lever,
        streams=streams,
        class_size=class_size,
        duration=duration,
        keys=keys,
        endpoint=endpoint,
        gateway=s4_weights.Gateway(endpoint, name, keys[0]),
        workspace=workspace,
    ).measure()


if __name__ == "__main__":
    main()
