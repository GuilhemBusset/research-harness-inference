"""Reads the session capacity of a run, and the rows that limit it, from its evidence
(docs/design/agent-session-evaluation.md, "Reading a result"; docs/measurements/s5.md).

Everything here is computed from typed records the controller hands in for each execution of a
cell (`CellEvidence`): the cell's `contracts.CellRecord`, its `contracts.RequestRecord`s, its
`contracts.HostSample`s, the scheduler's count of what it sent (`replay.Summary.tally`), and the
seconds each host was in service (`telemetry.Host.in_service_seconds`). It reads no file, no
clock and no network, so a saved bundle gives the same reading on any machine.

**What is read against what.** The floors are the deployed ones the plan's workload froze
(`objectives`): later-turn p95 time to first output under `ttft_p95_ms_max`, the all-stream p50
and p10 speeds at or above their floors, the failed share at or below `failed_share_max`, and no
preemption on any serving host. None is a constant of this module. A steady repetition needs at
least `MIN_SAMPLES` eligible later turns and as many eligible streams, or the workload's sampling
budget for each when it asks for more. The driver's tolerance is the suite's
(`driver.scheduling_lag_p95_ms_max`), which the plan's protocol versions.

**The class-size cells** are the capacity-sweep cells the plan gives the role `CLASS_FLOOR`: the
class's students times the conversations of a student in that shape, which is not the largest
cell of the sweep when the class is small. Each repetition of each shape is judged on its own,
and the shape's verdict comes from those verdicts, never from pooled samples: a repetition that
misses makes the shape a known miss whatever the other repetitions show. A shape passes only when
every planned repetition does; otherwise it is inconclusive. The spread of each number across the
repetitions is reported beside them.

**A repetition's verdict** is one of four (`Verdict`):

- `invalid`: the evidence cannot qualify (`Reason`): the record is incomplete, flagged, skipped an
  opening or left a start with no outcome; the driver lagged beyond the tolerance at p95,
  measured from each request's eligible time to its send, over the steady sends (warm-up traffic
  cannot dilute it) and over every send; the scheduler's tally is missing, has a
  send with no outcome, or does not reconcile with the request rows (their number, their distinct
  request ordinals, their outcomes, and the streams it found eligible: a request row does not say
  whether its output count closed the answer, the tally does); a request sent in the window ended
  after the drain, so the window has no outcome of it; the hosts the samples show are not the
  hosts in service, or not the number the record observed; or a serving host's preemption counter
  does not cover the window through drain.
- `measured_miss`: valid, and a floor that has its samples is missed. Time to first output needs
  its eligible later turns, the speeds their eligible streams, and the failed share and the
  preemptions, which are of the whole repetition, need both. A miss is kept when another floor of
  the same repetition lacks samples.
- `insufficient_evidence`: valid, nothing missed, and a floor lacks its samples, or no evidence of
  the cell was handed in at all. What was observed is still reported, and `observed_misses` names
  the floors it falls outside of, samples or not.
- `pass`: every floor met.

**Times and speeds.** Time to first output runs from the send to the first output-bearing event
(`first_output_ms`), of every request that had one, failed or not, as S5 reads it; time to the
first answer that is not reasoning is shown beside it. First turns (turn 0) and later turns are
read apart, and only later turns are held to the floor. A stream's speed is `(output_tokens - 1) / (last_output - first_output)` for a
completed request with at least two output tokens and two distinct times, as
`streams.Attempt.output_tokens_per_second` reads it. Percentiles are `common.percentile`.

**Failures** are every request that did not complete, midstream (a cut stream, an error event in
a 200) or before (a transport error, an HTTP refusal), over every attempt sent: a count, a share,
its Wilson 95% interval (a descriptive interval over requests) and the count by cause. With no
failure the share is an observed floor. Requests that reported no usage are counted apart. In a
steady cell, a request tagged as warm-up traffic is accounted for and left out of every reading:
it is not steady state. The drain lets admitted work finish: a conversation that opens after
admissions closed is in the attempts and the failures, and gives no time to first output or speed.

**Hosts.** A host of a cell is an ordinal its samples carry: telemetry keeps one for each host
across the run, so a cell's hosts need not start at 0. A counter is read over the window, from its
last sample at or before the start to its first at or after the end of the drain, each within
`2 * host_sample_interval_ms` (a scrape, and another for its delivery), with no two samples
further apart than that and no null between them. A gap leaves the counter missing, never zero.
Each rise is added, and a drop is a restart from zero whose new value is added in full, as S5's
`counted_of` reads it. Engine throughput is each host's output-token increase over the elapsed
seconds of the window through drain, the same window for every host, and the pool's is their sum;
delivered tokens are the output every request that had a first output reported, a failed one
included: a count with no output behind it (an opening usage before a cut) reached no one. The
cached share is the cached input over all input the engines counted, reported and not held to a
floor. Queue depth and GPU utilization are the mean and peak of each host's samples inside the
window; the pool's queue is read from its totals at each scrape, each host at its latest sample
within the allowance, so that its peak is of requests waiting at once. A null scrape is kept: with
no scrape where every host's latest sample has a value, the pool's queue is unknown.

**Host cost** is an estimate: the seconds each host was in service at the price the caller gives
for a host-hour, per million output tokens the engines generated. With no host time or no
generated token it is `None`, and its parts are still given.

**Outside steady state.** The warm-up and the class openings are read like any cell and marked
`steady_state = False`; they are never in a shape's verdict or its spread.

It needs `spikes` on the path, for `common.percentile`, and what `spikes/common.py` imports.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from statistics import NormalDist, fmean
from typing import TYPE_CHECKING

from common import percentile
from evaluate.contracts import CellStatus, HostMetric, InvalidFlag, RequestOutcome, Shape, Traffic
from evaluate.plan import Role

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from evaluate.contracts import CellIdentity, CellRecord, HostSample, RequestRecord
    from evaluate.plan import Plan, PlannedCell
    from evaluate.streams import Tally

# The fewest eligible later turns, and eligible streams, a steady repetition is read on.
MIN_SAMPLES = 100
HOUR_S = 3600
# The two-sided 95% normal quantile of the Wilson interval.
Z_95 = NormalDist().inv_cdf(0.975)
COUNTERS = (
    HostMetric.OUTPUT_TOKENS_TOTAL,
    HostMetric.INPUT_TOKENS_TOTAL,
    HostMetric.CACHED_INPUT_TOKENS_TOTAL,
    HostMetric.COMPUTED_INPUT_TOKENS_TOTAL,
    HostMetric.PREEMPTIONS_TOTAL,
)
GAUGES = (HostMetric.RUNNING_REQUESTS, HostMetric.WAITING_REQUESTS, HostMetric.GPU_UTILIZATION)


class Verdict(StrEnum):
    PASS = "pass"
    MEASURED_MISS = "measured_miss"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID = "invalid"


class Floor(StrEnum):
    TTFT_P95 = "ttft_p95"
    SPEED_P50 = "speed_p50"
    SPEED_P10 = "speed_p10"
    FAILED_SHARE = "failed_share"
    PREEMPTIONS = "preemptions"


class Check(StrEnum):
    """How one floor of one repetition reads."""

    MET = "met"
    MISSED = "missed"
    INSUFFICIENT = "insufficient"


class Reason(StrEnum):
    """Why a repetition's evidence cannot qualify."""

    RECORD_INCOMPLETE = "record_incomplete"
    DRIVER_SATURATION = "driver_saturation"
    EXTERNAL_TRAFFIC = "external_traffic"
    HOST_TURNOVER = "host_turnover"
    MISSING_EVIDENCE = "missing_evidence"
    UNRECONCILED_COUNTERS = "unreconciled_counters"
    SKIPPED_OPENING = "skipped_opening"
    OUTSTANDING_START = "outstanding_start"
    DRIVER_LAG = "driver_lag"
    ATTEMPTS_UNACCOUNTED = "attempts_unaccounted"
    ELIGIBILITY_UNRECONCILED = "eligibility_unreconciled"
    OUTCOME_AFTER_DRAIN = "outcome_after_drain"
    HOSTS_UNRECONCILED = "hosts_unreconciled"
    PREEMPTIONS_UNCOVERED = "preemptions_uncovered"


class ShapeVerdict(StrEnum):
    PASS = "pass"
    KNOWN_MISS = "known_miss"
    INCONCLUSIVE = "inconclusive"


class CapacityError(ValueError):
    """Evidence that is not of its plan. The message names the rule, never a value."""


@dataclass(frozen=True)
class CellEvidence:
    """One execution of a cell: its record, its requests and host samples, the scheduler's tally of
    its sends (`None` when none was handed in) and the seconds each host that served the cell was
    in service, by its ordinal: the hosts in service, which the samples must show."""

    record: CellRecord
    requests: tuple[RequestRecord, ...]
    samples: tuple[HostSample, ...]
    tally: Tally | None
    in_service_seconds: Mapping[int, float]


@dataclass(frozen=True)
class Latency:
    """Time to first output of the requests that had one, and to the first answer that is not reasoning."""

    count: int
    p50_ms: float | None
    p95_ms: float | None
    answer_count: int
    answer_p95_ms: float | None


@dataclass(frozen=True)
class Speeds:
    """The speed of each eligible stream, in output tokens a second."""

    count: int
    p50_tokens_per_s: float | None
    p10_tokens_per_s: float | None


@dataclass(frozen=True)
class Failures:
    """Failed attempts over every attempt, with the Wilson 95% interval of the share."""

    failed: int
    attempts: int
    share: float | None
    wilson_low: float | None
    wilson_high: float | None
    by_cause: Mapping[RequestOutcome, int]
    usage_missing: int

    @property
    def observed_floor(self) -> bool:
        """No failure was seen: the share is a floor of what was observed, not a rate."""
        return self.attempts > 0 and self.failed == 0


@dataclass(frozen=True)
class Level:
    mean: float | None
    peak: float | None


@dataclass(frozen=True)
class HostReading:
    """One host over the window through drain. A counter that does not cover it is `None`."""

    host: int
    counters: Mapping[HostMetric, float | None]
    gauges: Mapping[HostMetric, Level]
    output_tokens_per_s: float | None
    in_service_seconds: float | None


@dataclass(frozen=True)
class Engine:
    """What the pool's engines did over the window through drain, and the host cost estimate."""

    window_seconds: float
    hosts: tuple[HostReading, ...]
    output_tokens: float | None
    output_tokens_per_s: float | None
    delivered_output_tokens: int
    delivered_usage_missing: int
    input_tokens: float | None
    cached_input_tokens: float | None
    computed_input_tokens: float | None
    cached_share: float | None
    preemptions: float | None
    waiting_requests_mean: float | None
    waiting_requests_peak: float | None
    gpu_utilization_mean: float | None
    host_hours: float | None
    host_cost_estimate_usd: float | None
    host_cost_estimate_per_million_output_usd: float | None


@dataclass(frozen=True)
class CellReading:
    """One execution of a cell of the plan, or a planned one with no evidence (`record` is `None`)."""

    identity: CellIdentity
    steady_state: bool
    class_floor: bool
    record: CellRecord | None
    verdict: Verdict
    reasons: tuple[Reason, ...]
    floors: Mapping[Floor, Check]
    observed_misses: tuple[Floor, ...]
    """The floors what was observed falls outside of, whether or not it has its samples."""
    warm_up_requests: int
    """Requests tagged as warm-up traffic in a steady cell: accounted for, and in no reading."""
    opened_in_drain_requests: int
    """Requests of conversations that opened after admissions closed: failed attempts if they
    failed, and no sample of time to first output or speed."""
    ttft_samples_min: int
    stream_samples_min: int
    driver_lag_p95_ms: float | None
    later_turns: Latency
    first_turns: Latency
    speeds: Speeds
    failures: Failures
    engine: Engine | None

    @property
    def missed(self) -> tuple[Floor, ...]:
        return tuple(floor for floor, check in self.floors.items() if check is Check.MISSED)


@dataclass(frozen=True)
class Spread:
    """The lowest and highest value across the repetitions that have one."""

    low: float | None
    high: float | None


def _spread(values: Iterable[float | None]) -> Spread:
    known = [value for value in values if value is not None]
    return Spread(min(known, default=None), max(known, default=None))


@dataclass(frozen=True)
class ShapeReading:
    """A shape at the class size: each planned repetition, and the verdict they give together."""

    shape: Shape
    agent_slots: int
    repetitions: tuple[CellReading, ...]
    verdict: ShapeVerdict
    missed_repetitions: tuple[int, ...]
    ttft_p95_ms: Spread
    speed_p50_tokens_per_s: Spread
    speed_p10_tokens_per_s: Spread
    failed_share: Spread


@dataclass(frozen=True)
class Capacity:
    shapes: Mapping[Shape, ShapeReading]
    cells: tuple[CellReading, ...]

    @property
    def outside_steady_state(self) -> tuple[CellReading, ...]:
        return tuple(cell for cell in self.cells if not cell.steady_state)


# --- Requests -----------------------------------------------------------------------------------


def _at(samples: Sequence[float], percent: float) -> float | None:
    return percentile(samples, percent) if samples else None


def _latency(requests: Sequence[RequestRecord]) -> Latency:
    times = [float(one.first_output_ms - one.sent_ms) for one in requests if one.first_output_ms is not None]
    answers = [float(one.first_answer_ms - one.sent_ms) for one in requests if one.first_answer_ms is not None]
    return Latency(len(times), _at(times, 50), _at(times, 95), len(answers), _at(answers, 95))


def speed_of(request: RequestRecord) -> float | None:
    """The speed of an eligible stream, in output tokens a second; `None` for any other."""
    tokens = None if request.usage is None else request.usage.output_tokens
    first, last = request.first_output_ms, request.last_output_ms
    if request.failed or tokens is None or tokens < 2 or first is None or last is None or last <= first:
        return None
    return (tokens - 1) * 1000 / (last - first)


def _speeds(requests: Sequence[RequestRecord]) -> Speeds:
    speeds = [speed for one in requests if (speed := speed_of(one)) is not None]
    return Speeds(len(speeds), _at(speeds, 50), _at(speeds, 10))


def wilson(failed: int, attempts: int) -> tuple[float, float]:
    """The Wilson score 95% interval of `failed` out of `attempts`, which is not zero."""
    share, square = failed / attempts, Z_95 * Z_95
    scale = 1 + square / attempts
    centre = (share + square / (2 * attempts)) / scale
    half = Z_95 * math.sqrt(share * (1 - share) / attempts + square / (4 * attempts * attempts)) / scale
    return max(0.0, centre - half), min(1.0, centre + half)


def _failures(requests: Sequence[RequestRecord]) -> Failures:
    failed = sum(one.failed for one in requests)
    attempts = len(requests)
    low, high = wilson(failed, attempts) if attempts else (None, None)
    causes = [cause for cause in RequestOutcome if cause is not RequestOutcome.COMPLETED]
    return Failures(
        failed=failed,
        attempts=attempts,
        share=failed / attempts if attempts else None,
        wilson_low=low,
        wilson_high=high,
        by_cause={cause: sum(one.outcome is cause for one in requests) for cause in causes},
        usage_missing=sum(one.usage_missing for one in requests),
    )


def _accounted(requests: Sequence[RequestRecord], tally: Tally | None) -> bool:
    """Whether every send the scheduler counted has an outcome, and is a request row of its own
    and of that outcome: a failed request that was sent and not written cannot leave the
    denominator, and a row written twice cannot stand in for it."""
    if tally is None or tally.unfinished:
        return False
    rows = _failures(requests)
    completed = sum(not one.failed for one in requests)
    causes = {cause: tally.failures.get(cause, 0) for cause in rows.by_cause}
    distinct = len({one.request for one in requests}) == len(requests)
    counted = tally.attempts == len(requests) and tally.completed == completed
    return distinct and counted and causes == dict(rows.by_cause)


def _eligibility_reconciled(requests: Sequence[RequestRecord], tally: Tally | None) -> bool:
    """Whether the streams the rows give a speed are the ones the scheduler found eligible. A row
    has a speed whenever the scheduler's stream does, and can have one it does not (an output count
    that did not close the answer), so equal counts are the same streams."""
    return tally is None or tally.eligible == sum(speed_of(one) is not None for one in requests)


# --- Hosts --------------------------------------------------------------------------------------


def _increase(values: Sequence[float]) -> float:
    """A counter's increase over its samples: each rise added, each drop a restart from zero."""
    added = 0.0
    for before, after in pairwise(values):
        added += after - before if after >= before else after
    return added


def _counter(samples: Sequence[HostSample], record: CellRecord, allowance: int) -> float | None:
    """The increase of one counter of one host over the window through drain, `None` unless its
    samples cover it: one at or before the start and one at or after the drain, each within the
    allowance, none further apart than it, and no null between them."""
    ordered = sorted(samples, key=lambda sample: sample.at_ms)
    before = [at for at, sample in enumerate(ordered) if sample.at_ms <= record.started_ms]
    after = [at for at, sample in enumerate(ordered) if sample.at_ms >= record.ended_ms]
    if not before or not after:
        return None
    span = ordered[before[-1] : after[0] + 1]
    edges = record.started_ms - span[0].at_ms <= allowance and span[-1].at_ms - record.ended_ms <= allowance
    gaps = all(later.at_ms - earlier.at_ms <= allowance for earlier, later in pairwise(span))
    values = [sample.value for sample in span]
    if not edges or not gaps or any(value is None for value in values):
        return None
    return _increase([value for value in values if value is not None])


def _gauge(samples: Sequence[HostSample], record: CellRecord) -> Level:
    inside = [
        sample.value
        for sample in samples
        if sample.value is not None and record.started_ms <= sample.at_ms <= record.ended_ms
    ]
    return Level(fmean(inside) if inside else None, max(inside, default=None))


def _pool_level(by_host: Sequence[Sequence[HostSample]], record: CellRecord, allowance: int) -> Level:
    """A gauge of the pool: at each scrape of the window, the sum of each host's latest sample, when
    every host has one within the allowance and none of them is a null scrape. With no such scrape
    the pool's level is unknown; each host's own samples are still read."""
    series = [
        sorted(
            (
                (sample.at_ms, sample.value)
                for sample in samples
                if record.started_ms <= sample.at_ms <= record.ended_ms
            ),
            key=lambda pair: pair[0],
        )
        for samples in by_host
    ]
    totals = []
    for at in sorted({at for one in series for at, _ in one}):
        latest = []
        for one in series:
            index = bisect_right([when for when, _ in one], at)
            if index and at - one[index - 1][0] <= allowance and (value := one[index - 1][1]) is not None:
                latest.append(value)
        if series and len(latest) == len(series):
            totals.append(sum(latest))
    return Level(fmean(totals) if totals else None, max(totals, default=None))


def _sum(values: Sequence[float | None]) -> float | None:
    return None if not values or any(value is None for value in values) else sum(v for v in values if v is not None)


def _engine(evidence: CellEvidence, requests: Sequence[RequestRecord], plan: Plan, usd_per_host_hour: float) -> Engine:
    record = evidence.record
    allowance = 2 * plan.workload.sampling.host_sample_interval_ms
    seconds = (record.ended_ms - record.started_ms) / 1000
    samples = [sample for sample in evidence.samples if sample.cell == record.cell]
    hosts = []
    for host in sorted({sample.host for sample in samples} | set(evidence.in_service_seconds)):
        of = {
            metric: [sample for sample in samples if sample.host == host and sample.metric is metric]
            for metric in HostMetric
        }
        counters = {metric: _counter(of[metric], record, allowance) for metric in COUNTERS}
        output = counters[HostMetric.OUTPUT_TOKENS_TOTAL]
        hosts.append(
            HostReading(
                host=host,
                counters=counters,
                gauges={metric: _gauge(of[metric], record) for metric in GAUGES},
                output_tokens_per_s=None if output is None or not seconds else output / seconds,
                in_service_seconds=evidence.in_service_seconds.get(host),
            )
        )

    def total(metric: HostMetric) -> float | None:
        return _sum([host.counters[metric] for host in hosts])

    output, cached, whole = (
        total(HostMetric.OUTPUT_TOKENS_TOTAL),
        total(HostMetric.CACHED_INPUT_TOKENS_TOTAL),
        total(HostMetric.INPUT_TOKENS_TOTAL),
    )
    service = _sum([host.in_service_seconds for host in hosts])
    host_hours = None if service is None else service / HOUR_S
    cost = None if host_hours is None else host_hours * usd_per_host_hour
    waiting = _pool_level(
        [
            [one for one in samples if one.host == host.host and one.metric is HostMetric.WAITING_REQUESTS]
            for host in hosts
        ],
        record,
        allowance,
    )
    gpu = [host.gauges[HostMetric.GPU_UTILIZATION].mean for host in hosts]
    counts = [None if one.usage is None else one.usage.output_tokens for one in requests]
    output_seen = [count for count, one in zip(counts, requests, strict=True) if one.first_output_ms is not None]
    return Engine(
        window_seconds=seconds,
        hosts=tuple(hosts),
        output_tokens=output,
        output_tokens_per_s=None if output is None or not seconds else output / seconds,
        delivered_output_tokens=sum(tokens for tokens in output_seen if tokens is not None),
        delivered_usage_missing=sum(tokens is None for tokens in output_seen),
        input_tokens=whole,
        cached_input_tokens=cached,
        computed_input_tokens=total(HostMetric.COMPUTED_INPUT_TOKENS_TOTAL),
        cached_share=cached / whole if cached is not None and whole else None,
        preemptions=total(HostMetric.PREEMPTIONS_TOTAL),
        waiting_requests_mean=waiting.mean,
        waiting_requests_peak=waiting.peak,
        gpu_utilization_mean=fmean(known) if (known := [mean for mean in gpu if mean is not None]) else None,
        host_hours=host_hours,
        host_cost_estimate_usd=cost,
        host_cost_estimate_per_million_output_usd=cost / (output / 1e6) if cost is not None and output else None,
    )


# --- A cell -------------------------------------------------------------------------------------

_FLAG_REASONS = {flag: Reason(flag.value) for flag in InvalidFlag}


def _reasons(
    evidence: CellEvidence,
    requests: Sequence[RequestRecord],
    engine: Engine,
    lags: Sequence[float | None],
    tolerance: float,
) -> tuple[Reason, ...]:
    record = evidence.record
    found = [_FLAG_REASONS[flag] for flag in record.invalid_flags]
    if record.status is not CellStatus.COMPLETE:
        found.append(Reason.RECORD_INCOMPLETE)
    if record.conversations_skipped:
        found.append(Reason.SKIPPED_OPENING)
    if record.conversations_outstanding:
        found.append(Reason.OUTSTANDING_START)
    lags = [value for value in (*lags, record.driver_lag_p95_ms) if value is not None]
    if any(value > tolerance for value in lags):
        found.append(Reason.DRIVER_LAG)
    if not _accounted(requests, evidence.tally):
        found.append(Reason.ATTEMPTS_UNACCOUNTED)
    if not _eligibility_reconciled(requests, evidence.tally):
        found.append(Reason.ELIGIBILITY_UNRECONCILED)
    if any(one.ended_ms > record.ended_ms for one in requests):
        found.append(Reason.OUTCOME_AFTER_DRAIN)
    sampled = {sample.host for sample in evidence.samples if sample.cell == record.cell}
    serving = set(evidence.in_service_seconds)
    if record.hosts_observed is None or record.hosts_observed != len(engine.hosts) or sampled != serving:
        found.append(Reason.HOSTS_UNRECONCILED)
    preemptions = [host.counters[HostMetric.PREEMPTIONS_TOTAL] for host in engine.hosts]
    if not preemptions or any(value is None for value in preemptions):
        found.append(Reason.PREEMPTIONS_UNCOVERED)
    return tuple(dict.fromkeys(found))


def _floors(
    plan: Plan, later: Latency, speeds: Speeds, failures: Failures, engine: Engine | None
) -> tuple[dict[Floor, Check], tuple[Floor, ...]]:
    """Each floor's check, and the floors what was observed falls outside of, samples or not."""
    objectives = plan.workload.objectives
    ttft_min, stream_min = _minimums(plan)
    p95, p50, p10, share = later.p95_ms, speeds.p50_tokens_per_s, speeds.p10_tokens_per_s, failures.share
    preemptions = None if engine is None else engine.preemptions
    observed = {
        Floor.TTFT_P95: None if p95 is None else p95 < objectives.ttft_p95_ms_max,
        Floor.SPEED_P50: None if p50 is None else p50 >= objectives.stream_speed_p50_tokens_per_s_min,
        Floor.SPEED_P10: None if p10 is None else p10 >= objectives.stream_speed_p10_tokens_per_s_min,
        Floor.FAILED_SHARE: None if share is None else share <= objectives.failed_share_max,
        Floor.PREEMPTIONS: None if preemptions is None else preemptions == 0,
    }
    turns, streams = later.count >= ttft_min, speeds.count >= stream_min
    enough = {
        Floor.TTFT_P95: turns,
        Floor.SPEED_P50: streams,
        Floor.SPEED_P10: streams,
        # Of the whole repetition: read once it has its samples.
        Floor.FAILED_SHARE: turns and streams,
        Floor.PREEMPTIONS: turns and streams,
    }
    floors = {
        floor: Check.INSUFFICIENT if not enough[floor] or met is None else Check.MET if met else Check.MISSED
        for floor, met in observed.items()
    }
    return floors, tuple(floor for floor, met in observed.items() if met is False)


def _verdict(reasons: Sequence[Reason], floors: Mapping[Floor, Check]) -> Verdict:
    if reasons:
        return Verdict.INVALID
    checks = set(floors.values())
    if Check.MISSED in checks:
        return Verdict.MEASURED_MISS
    return Verdict.INSUFFICIENT_EVIDENCE if Check.INSUFFICIENT in checks else Verdict.PASS


def _minimums(plan: Plan) -> tuple[int, int]:
    sampling = plan.workload.sampling
    return max(MIN_SAMPLES, sampling.ttft_samples_min), max(MIN_SAMPLES, sampling.stream_samples_min)


def _lag(requests: Sequence[RequestRecord]) -> float | None:
    """The driver's scheduling lag at p95: each send against the time it was eligible."""
    return _at([float(one.sent_ms - one.eligible_ms) for one in requests], 95)


def _admitted(requests: Sequence[RequestRecord], record: CellRecord) -> list[RequestRecord]:
    """The requests of conversations admitted before admissions closed. The drain lets admitted work
    finish: a conversation that opened in it (its first turn eligible after admissions ended) is
    accounted for and gives no sample."""
    late = {
        (one.student, one.conversation)
        for one in requests
        if one.turn == 0 and one.eligible_ms > record.admissions_ended_ms
    }
    return [one for one in requests if (one.student, one.conversation) not in late]


def _cell(planned: PlannedCell, evidence: CellEvidence | None, plan: Plan, usd_per_host_hour: float) -> CellReading:
    ttft_min, stream_min = _minimums(plan)
    requests: list[RequestRecord] = []
    read: list[RequestRecord] = []
    sampled: list[RequestRecord] = []
    engine = None
    lag = None
    reasons: tuple[Reason, ...] = ()
    if evidence is not None:
        record = evidence.record
        requests = [
            one
            for one in evidence.requests
            if one.cell == record.cell and record.started_ms <= one.sent_ms <= record.ended_ms
        ]
        # Warm-up traffic in a steady cell is accounted for, and is not steady state.
        read = [one for one in requests if not (planned.steady_state and one.traffic is Traffic.WARM_UP)]
        sampled = _admitted(read, record)
        # The steady sends are the driver's steady-state lag; every send is held to the tolerance too.
        lag = _lag(read)
        engine = _engine(evidence, read, plan, usd_per_host_hour)
        tolerance = float(plan.spec.protocol["driver"]["scheduling_lag_p95_ms_max"])
        reasons = _reasons(evidence, requests, engine, (lag, _lag(requests)), tolerance)
    later, first = (
        _latency([one for one in sampled if one.turn > 0]),
        _latency([one for one in sampled if one.turn == 0]),
    )
    speeds, failures = _speeds(sampled), _failures(read)
    floors, observed_misses = _floors(plan, later, speeds, failures, engine)
    verdict = Verdict.INSUFFICIENT_EVIDENCE if evidence is None else _verdict(reasons, floors)
    return CellReading(
        identity=planned.identity,
        steady_state=planned.steady_state,
        class_floor=Role.CLASS_FLOOR in planned.roles,
        record=None if evidence is None else evidence.record,
        verdict=verdict,
        reasons=reasons,
        floors=floors,
        observed_misses=observed_misses,
        warm_up_requests=len(requests) - len(read),
        opened_in_drain_requests=len(read) - len(sampled),
        ttft_samples_min=ttft_min,
        stream_samples_min=stream_min,
        driver_lag_p95_ms=lag,
        later_turns=later,
        first_turns=first,
        speeds=speeds,
        failures=failures,
        engine=engine,
    )


def _measured(plan: Plan, evidence: Iterable[CellEvidence]) -> dict[int, CellEvidence]:
    """The execution each cell is read on: its one valid record, or else its last one."""
    executions: dict[int, list[CellEvidence]] = {}
    for one in evidence:
        if one.record.cell >= len(plan.cells):
            raise CapacityError("a cell record is of a cell of the plan")
        executions.setdefault(one.record.cell, []).append(one)
    chosen = {}
    for cell, kept in executions.items():
        valid = [one for one in kept if one.record.valid_for_qualification]
        if len(valid) > 1:
            raise CapacityError("a cell is measured once")
        chosen[cell] = valid[0] if valid else max(kept, key=lambda one: one.record.started_ms)
    return chosen


def _shape(shape: Shape, repetitions: Sequence[CellReading]) -> ShapeReading:
    verdicts = [cell.verdict for cell in repetitions]
    missed = tuple(cell.identity.repetition for cell in repetitions if cell.verdict is Verdict.MEASURED_MISS)
    if missed:
        verdict = ShapeVerdict.KNOWN_MISS
    elif verdicts and all(one is Verdict.PASS for one in verdicts):
        verdict = ShapeVerdict.PASS
    else:
        verdict = ShapeVerdict.INCONCLUSIVE
    return ShapeReading(
        shape=shape,
        agent_slots=repetitions[0].identity.agent_slots,
        repetitions=tuple(repetitions),
        verdict=verdict,
        missed_repetitions=missed,
        ttft_p95_ms=_spread(cell.later_turns.p95_ms for cell in repetitions),
        speed_p50_tokens_per_s=_spread(cell.speeds.p50_tokens_per_s for cell in repetitions),
        speed_p10_tokens_per_s=_spread(cell.speeds.p10_tokens_per_s for cell in repetitions),
        failed_share=_spread(cell.failures.share for cell in repetitions),
    )


def read(plan: Plan, evidence: Iterable[CellEvidence], *, usd_per_host_hour: float) -> Capacity:
    """The capacity of a run: a reading of every cell that has evidence and of every class-size
    cell of the plan, and the verdict of each shape at the class size. `usd_per_host_hour` is the
    configured price of one host for one hour, which the host cost is estimated with."""
    measured = _measured(plan, evidence)
    cells = [
        _cell(planned, measured.get(planned.identity.cell), plan, usd_per_host_hour)
        for planned in plan.cells
        if planned.identity.cell in measured or Role.CLASS_FLOOR in planned.roles
    ]
    shapes = {}
    for shape in Shape:
        floor = [cell for cell in cells if cell.class_floor and cell.steady_state and cell.identity.shape is shape]
        if floor:
            shapes[shape] = _shape(shape, sorted(floor, key=lambda cell: cell.identity.repetition))
    return Capacity(shapes=shapes, cells=tuple(cells))
