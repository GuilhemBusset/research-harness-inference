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

The students are the run's label. Every student is sent as a canary student of its own, with its
own key (`common.canary_keys`), as the load generator sends them (`loadgen.replay`, which keys each
session by its student): its own cache salt and limits, and its own in the records; the sessions of
a several-agents student share their student's key, as a real student's agents would. A run needs a
key for each of its students, and the job refuses, before it sends anything, runs that need more
keys than the spikes' secret holds.

**One run** first wakes the pool through the gateway and waits until it is ready, so that a run
measures a warm pool, never a cold start. It then replays its sessions (`loadgen.agent_session`, of
section 4.7's sizes) for the duration it is given: no session starts a pause that would end after
the duration (`Until`), none sends a request once the duration has passed, however late a pause
ends (`loadgen.Load.sending`), and the run ends when the requests in flight have. A session holds
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
they are summed per instance: an instance's counters restart from zero with its engine. They are
read one scrape after the run's end, from the run's start to then, with Prometheus' `anchored`
range modifier, over a range to the millisecond that starts at the run's start: the increase starts
from the last sample at or before the run's start, what each counter held when the run began, so
what the run added before its first scrape is counted, nothing from before the run is, and it is
not extended past the samples it saw. A series with no sample within the lookback before the run's
start (`last_over_time` over it, read at the start itself, which a stale marker does not hide), an
instance that joined during it, began at zero with its engine: its first sample is added to its
increase, which `anchored` takes from that sample (`counted_of`); a series with one is not, whatever
time its first point in the window carries, even one that went stale and resumed in the run, whose
increase is anchored on the sample before its stale marker. The quantiles are computed from
the buckets so counted (`quantile_of`), as `histogram_quantile` would. The scrape after the run's
end holds the tokens of the last answers, which the workspace sees only then; nothing else is sent
to the pool in it. The gauges are read over the run alone, on a grid of a second (`fine_step`) that
ends at the run's end, so that every scrape of the run shows, however short the run. Rates are over
the run's seconds.

**What each instance's lifecycle agent reported**: the cache capacity in tokens the engine gave at
start (`rhi_lifecycle.cache.METRIC`), the last reported from a scrape before the run to a scrape
after it, and its release (`rhi_lifecycle.serving.RELEASE_METRIC`), which an instance writes only
while in service: the build, its `model_revision`, and from the times of its samples, the seconds
the instance was in service during the window, within a scrape of a sample, so that a warm instance
stopped between two periods of service is not charged for the gap. The run reports the instances
that served in it, and only those. The samples are read on the same fine grid to one
scrape past the window's end, so that an instance in service at the end is seen there, and a run
shorter than a scrape, whose instances are first scraped after it ends, is still labelled. An
instance whose last sample is older than a scrape before the window, which the workspace still
returns for some minutes, or whose first is a whole scrape after it, did not serve in it and is
left out, as is a release an instance reported only that far from the window. A pool whose
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
import statistics
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
# Seconds. Prometheus' lookback delta, its default: how far back before a range `anchored` looks
# for the sample it starts an increase from.
LOOKBACK_SECONDS = 300
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
    """The sessions of one round of `planned`, for the pool `model` names, drawn from `seed` on,
    each student's sessions sent as that canary student."""
    if planned.shape is Shape.HANDS_ON:
        return loadgen.hands_on(model, planned.students, turns=TURNS_PER_SESSION, seed=seed)
    load = loadgen.several_agents(model, planned.students, turns=TURNS_PER_SESSION, seed=seed)
    return loadgen.Load(load.sessions[: planned.streams], load.sessions_per_student)


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
    (`release_query`, read to a scrape past `end`): in service within a scrape of each sample, since
    a sample shows the instance in service at some time in the scrape before it, so from a scrape
    before its first sample to a scrape after its last, inside the window, but not across a gap in
    its samples longer than two scrapes, a warm instance stopped and started again. Only the
    releases whose samples so give some seconds in the window count: an instance with none did not
    serve in the window and is not in the answer, and a release it reported only a whole scrape
    before or after the window, one it served outside the run, labels nothing; one first scraped
    after a short window ended still labels it. ValueError for an instance whose releases in the
    window carry no instance type, or are two."""
    times: dict[str, dict[tuple[str, str], list[float]]] = {}
    for one in release:
        instance = one.labels.get("instance", "")
        if instance:
            kind = (one.labels.get("instance_type", ""), one.labels.get("model_revision", ""))
            times.setdefault(instance, {}).setdefault(kind, []).extend(one.values())
    found = {}
    for instance, by_kind in times.items():
        served = {kind: _covered(taken, start, end) for kind, taken in by_kind.items()}
        served = {kind: seconds for kind, seconds in served.items() if seconds > 0}
        if not served:
            continue
        if len(served) != 1 or not all(next(iter(served))):
            raise ValueError("an instance reported no instance type, no build, or two of either in the window")
        (((kind, build), seconds),) = served.items()
        found[instance] = InService(kind, build, seconds)
    return found


def _covered(taken: Sequence[float], start: float, end: float) -> float:
    """The seconds from `start` to `end` within a scrape either side of a time of `taken`: their
    union, inside the window."""
    seconds, covered = 0.0, start
    for at in sorted(set(taken)):
        low, high = max(covered, at - SCRAPE_SECONDS), min(end, at + SCRAPE_SECONDS)
        if high > low:
            seconds, covered = seconds + high - low, high
    return seconds


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


def _range(seconds: float) -> str:
    """A range of `seconds` to the millisecond, so that it starts where the run did, not at a whole
    second before it: in seconds when they are whole, else in milliseconds."""
    millis = max(1, round(seconds * 1000))
    return f"{millis // 1000}s" if millis % 1000 == 0 else f"{millis}ms"


class How(StrEnum):
    """How the answer to one query becomes one number an instance."""

    SUM = "sum"
    """An increase, read at one time, with the series born in the run counted from zero."""
    QUANTILE = "quantile"
    """A histogram's bucket increases, so read, then its quantile at `Read.fraction`."""
    MEAN = "mean"
    """A gauge, read over the run on a fine grid: the mean of its points."""
    PEAK = "peak"
    """The same: the largest of its points."""
    LAST = "last"
    """A number read at one time as it is."""


@dataclass(frozen=True)
class Read:
    """One query of the engine's quantities, and how its answer becomes a number an instance. `raw`
    is the selector of the series an increase sums, for those born in the run."""

    query: str
    how: How
    raw: str = ""
    fraction: float = 0.0


def reads(pool: str, engine: str, window_seconds: float, counted_seconds: float | None = None) -> dict[Key, Read]:
    """The reads of the engine's quantities on the pool, by the names of `engine`'s row; a further
    quantity the row leaves out has none. The counters and histograms are increased over the
    `counted_seconds` that end where they are read, the window's unless given; the lifecycle agent's
    cache capacity is the last it reported over those seconds and a scrape before them; the gauges
    are read over the run as they are."""
    names, further = metric_names(engine), further_names(engine)
    counted_seconds = window_seconds if counted_seconds is None else counted_seconds
    counted = _range(counted_seconds)

    def increase(name: Name, suffix: str = "", by: str = "instance") -> Read:
        if not suffix:
            suffix = "_total" if name.kind is Kind.COUNTER else ""
        raw = _selector(name, suffix, pool)
        return Read(f"sum by ({by}) (increase({raw}[{counted}] anchored))", How.SUM, raw)

    def quantile(fraction: float, name: Name) -> Read:
        buckets = increase(name, "_bucket", "instance, le")
        return Read(buckets.query, How.QUANTILE, buckets.raw, fraction)

    def gauge(how: How, aggregate: str, name: Name) -> Read:
        return Read(f"{aggregate} by (instance) ({_selector(name, '', pool)})", how)

    ttft, tpot = names[Quantity.TIME_TO_FIRST_TOKEN], names[Quantity.TIME_PER_OUTPUT_TOKEN]
    capacity = f'{CACHE_CAPACITY_METRIC}{{pool="{pool}"}}[{_range(counted_seconds + SCRAPE_SECONDS)}]'
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
        Key.RUNNING_MEAN: gauge(How.MEAN, "sum", names[Quantity.RUNNING_REQUESTS]),
        Key.RUNNING_PEAK: gauge(How.PEAK, "sum", names[Quantity.RUNNING_REQUESTS]),
        Key.WAITING_MEAN: gauge(How.MEAN, "sum", names[Quantity.WAITING_REQUESTS]),
        Key.WAITING_PEAK: gauge(How.PEAK, "sum", names[Quantity.WAITING_REQUESTS]),
        Key.CACHE_USE_MEAN: gauge(How.MEAN, "max", names[Quantity.CACHE_USAGE]),
        Key.CACHE_USE_PEAK: gauge(How.PEAK, "max", names[Quantity.CACHE_USAGE]),
        Key.CACHE_CAPACITY: Read(f"max by (instance) (last_over_time({capacity}))", How.LAST),
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


def queries(pool: str, engine: str, window_seconds: float, counted_seconds: float | None = None) -> dict[Key, str]:
    """The query of each read (`reads`): a quantile's is the bucket increases it is computed from."""
    return {key: one.query for key, one in reads(pool, engine, window_seconds, counted_seconds).items()}


def _where(one: common.Series) -> tuple[str, str]:
    return one.labels.get("instance", ""), one.labels.get("le", "")


def _identity(one: common.Series) -> frozenset[tuple[str, str]]:
    """Which raw series `one` is: all its labels but the name, which `last_over_time` drops. An
    instance's engines are series of their own (vLLM labels each with its engine index)."""
    return frozenset((label, value) for label, value in one.labels.items() if label != "__name__")


def _held(one: common.Series, there: Sequence[frozenset[tuple[str, str]]]) -> bool:
    """Whether the start held raw series `one`: a series at the start whose labels are all `one`'s,
    which is `one` itself, its name aside, as `last_over_time` answers."""
    labels = _identity(one)
    return any(held <= labels for held in there)


def counted_of(
    increases: Sequence[common.Series], raw: Sequence[common.Series], at_start: Sequence[common.Series]
) -> dict[tuple[str, str], float]:
    """Each instance's increase, by bucket (`le`, empty for none): the anchored increases, plus, for
    each series of `raw` (read over the window) that `at_start` does not hold, matched on all its
    labels but the name (one engine's series is not anchored on another's of the same instance),
    its first value. `at_start` is the selector's last sample within the lookback, read at the
    window's start, as `anchored` looks for the sample it starts from; a stale marker does not hide
    it. A series without one began at zero with its engine, and with nothing to anchor on, its
    increase is taken from its first sample, which would leave out what the engine counted before
    it was first scraped. A series `at_start` holds is anchored on the value it held then, which is
    counted once, also when it went stale before the start and resumed after it. The answers carry
    the times they were evaluated at, not the times of the samples, so whether a series was there
    at the start is read at the start, never from the time of its first point."""
    totals: dict[tuple[str, str], float] = {}
    there = [_identity(one) for one in at_start if one.samples and one.labels.get("instance")]

    def add(one: common.Series, value: float) -> None:
        where = _where(one)
        if where[0]:
            totals[where] = totals.get(where, 0.0) + value

    for one in increases:
        if one.samples:
            add(one, one.samples[-1][1])
    for one in raw:
        if one.samples and not _held(one, there):
            add(one, min(one.samples)[1])
    return totals


def quantile_of(fraction: float, buckets: Mapping[str, float]) -> float | None:
    """The quantile at `fraction` of one histogram's cumulative bucket counts by their upper bound,
    as Prometheus' `histogram_quantile` interpolates it. None for no `+Inf` bucket or no count."""
    bounds = sorted((float(le), count) for le, count in buckets.items())
    if len(bounds) < 2 or bounds[-1][0] != math.inf:
        return None
    cumulative, most = [], 0.0
    for bound, count in bounds:
        most = max(most, count)
        cumulative.append((bound, most))
    total = cumulative[-1][1]
    if total <= 0:
        return None
    rank = fraction * total
    at = next(index for index, (_, count) in enumerate(cumulative) if count >= rank)
    if at == len(cumulative) - 1:
        return cumulative[-2][0]
    upper, count = cumulative[at]
    if at == 0 and upper <= 0:
        return upper
    lower, below = cumulative[at - 1] if at else (0.0, 0.0)
    return lower + (upper - lower) * (rank - below) / (count - below)


def fine_step(seconds: float) -> float:
    """A range query's step finer than a scrape, so every scrape shows: a second, or the coarsest
    the workspace's 11,000 points a series allow."""
    return float(max(1, math.ceil(seconds / 10_000)))


def grid_start(start: float, end: float, step: float) -> float:
    """Where a range query from `start` begins so that `end` is on its grid: `start` or a step less
    after it."""
    return end - math.floor((end - start) / step) * step


def grid_before(start: float, end: float, step: float) -> float:
    """Where a range query from `start` begins so that `end` is on its grid and its first point is
    at `start` or before it: `start` or less than a step before it. A series already scraped by
    then shows at that first point, and only one born after `start` first shows after it."""
    return end - math.ceil((end - start) / step) * step


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

    instance_type: str
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

    def _keyed(self, runs: Sequence[Planned]) -> None:
        """ValueError for a run with more students than there are canary students' keys."""
        if max(run.students for run in runs) > len(self.keys):
            raise ValueError("a run needs a canary student's key for each student, and there are fewer keys")

    def _replay(self, planned: Planned, deadline: float) -> list[loadgen.Record]:
        records: list[loadgen.Record] = []

        def sending() -> bool:
            return self.wall() < deadline

        while sending():
            load = load_of(planned, self.pool, self._seed)
            self._seed += len(load.sessions)
            bounded = loadgen.Load(
                tuple(Until(session, deadline, self.wall) for session in load.sessions),
                load.sessions_per_student,
                sending,
            )
            began = self.wall()
            round_records = asyncio.run(self._send(bounded))
            records += round_records
            # A round in which no time passed would be repeated, on a clock the requests do not
            # move, for ever: it is the last.
            if not round_records or self.wall() <= began:
                break
        return records

    async def _send(self, load: loadgen.Load) -> list[loadgen.Record]:
        return await self.replay(load, self.keys, self.endpoint, at_once=len(load.sessions))

    def _read(self, query: str, start: float, end: float, step: float) -> list[common.Series]:
        return self.workspace.read_series(query, start=start, end=end, step=step)

    def _engine(self, start: float, end: float) -> dict[Key, list[common.Series]]:
        """The workspace's answers to `reads` for the run from `start` to `end`, each made one
        number an instance at the time it was read, as `quantities_of` takes them."""
        after = end + SCRAPE_SECONDS
        read_at = {after: fine_step(after - start), end: fine_step(end - start)}
        held: dict[tuple[str, float, float], list[common.Series]] = {}

        def read(query: str, low: float, high: float, step: float) -> list[common.Series]:
            if (query, low, high) not in held:
                held[query, low, high] = self._read(query, low, high, step)
            return held[query, low, high]

        def one(instance: str, at: float, value: float, le: str = "") -> common.Series:
            return common.Series({"instance": instance, **({"le": le} if le else {})}, ((at, value),))

        answers: dict[Key, list[common.Series]] = {}
        for key, how in reads(self.pool, self.engine, max(end - start, 1.0), after - start).items():
            if how.how is How.LAST:
                answers[key] = read(how.query, after, after, SCRAPE_SECONDS)
            elif how.how in (How.MEAN, How.PEAK):
                step = read_at[end]
                points: dict[str, list[float]] = {}
                for series in read(how.query, grid_start(start, end, step), end, step):
                    instance = series.labels.get("instance", "")
                    inside = [value for at, value in series.samples if start <= at <= end]
                    if instance and inside:
                        points.setdefault(instance, []).extend(inside)
                summary = statistics.fmean if how.how is How.MEAN else max
                answers[key] = [one(instance, end, summary(values)) for instance, values in points.items()]
            else:
                step = read_at[after]
                raw = read(how.raw, grid_before(start, after, step), after, step)
                there = read(f"last_over_time({how.raw}[{_range(LOOKBACK_SECONDS)}])", start, start, step)
                counted = counted_of(read(how.query, after, after, SCRAPE_SECONDS), raw, there)
                if how.how is How.SUM:
                    answers[key] = [one(instance, after, value, le) for (instance, le), value in counted.items()]
                    continue
                buckets: dict[str, dict[str, float]] = {}
                for (instance, le), value in counted.items():
                    buckets.setdefault(instance, {})[le] = value
                found = {instance: quantile_of(how.fraction, counts) for instance, counts in buckets.items()}
                answers[key] = [one(instance, after, value) for instance, value in found.items() if value is not None]
        return answers

    def run(self, planned: Planned) -> Measured:
        """One run: the pool ready, the sessions replayed for the duration, then what it measured.
        ValueError, with nothing sent, for a run with more students than there are keys."""
        self._keyed([planned])
        self._ready()
        start = self.wall()
        records = self._replay(planned, start + self.duration)
        end = self.wall()
        self.sleep(SETTLE_SECONDS)
        window = max(end - start, 1.0)
        # The scrape after the run's end holds what the run's last answers added; the counters'
        # increase is anchored on what they held at the run's start.
        after = end + SCRAPE_SECONDS
        step = fine_step(after - start)
        release = self._read(release_query(self.pool), grid_start(start, after, step), after, step)
        in_service = in_service_of(release, start, end)
        build = build_of(in_service)
        engine = quantities_of(self._engine(start, end), window)
        experience = experience_of(records)
        # The instances that served the run, and only those: one the workspace reads after the run,
        # or that had stopped before it, measured nothing of it.
        instances = {
            instance: Instance(
                instance_type=served.instance_type,
                seconds_in_service=served.seconds,
                engine=engine.get(instance, EngineQuantities()),
            )
            for instance, served in sorted(in_service.items())
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
        """Every run, in the plan's order, each printed as one JSON line once measured. ValueError,
        with nothing sent, for runs with more students than there are keys."""
        self._keyed(self.runs)
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
