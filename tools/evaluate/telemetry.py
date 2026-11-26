"""Reads what a pool served over a measured window, and says what of it the telemetry establishes
(docs/design/agent-session-evaluation.md, "Reproducing and comparing runs").

A release that was requested describes a desired state. What served a cell is read from the
Prometheus workspace afterwards: which hosts were in service, what each one ran, what each
engine counted, and who else reached the pool through the gateway. `read` asks the workspace,
`resolve` turns its answers into a `Reading`, and `preflight` asks before a run whether that
evidence can be read at all. Nothing here sends a request to the pool or keeps a clock: the
driver gives the window's times.

**The readers it stands on** are spike S5's (`s5_capacity`): its `Reader`, whose `read_samples`
gives each series' samples as they were scraped; `counted_of`, its increase of a counter, each
drop counted as a restart from zero; `seconds_covered`, the seconds a series' samples show; and
its `selector` and `range_of`. The names are those of the engine's row
(`rhi_common.engine_metrics`), of the lifecycle agent (`rhi_lifecycle.serving`,
`rhi_lifecycle.cache`), of the gateway (`rhi_gateway.metrics`) and of the GPU exporter
(`rhi_dashboards.gpu`).

**The window** runs from the cell's start to the end of its drain, in seconds since the epoch.
Every rate is over those seconds, the ones that elapsed, however long the drain was planned to
last. Every series is read from before the window (and before the warm-up, when one is given) to
`ALLOWANCE_SECONDS` after the drain, and it takes a moment for the last scrapes to reach the
workspace: read no sooner than `readable_from` says.

**What a series says of a window** is in the samples that bound it: from its last sample at or
before the start to its first at or after the drain. A scrape that fell on the drain closes the
window; otherwise the next one does, since it holds what the last answers added. No later sample
adds anything. Those samples cover the window when the first is within `ALLOWANCE_SECONDS`
before the start, the last is at or after the drain, and no two that follow each other are
further apart than `ALLOWANCE_SECONDS`. A sample that is no number is no sample.

**The hosts** are the instances whose lifecycle agent reported a release
(`rhi_lifecycle_release_info`) for some seconds of the window, in service within a scrape of
each sample as in S5, and every other instance of which a series of the pool has a sample in the
window. A host served the whole window when its release samples start and end within
`ALLOWANCE_SECONDS` of the window's edges with no hole longer than that between them
(`Host.membership`). One that started later or ended earlier joined or left: the cell had a
turnover of hosts. One with a hole is of incomplete coverage, since nothing shows it stayed. One
with no release sample at all, whose engine counted all the same, is not observed: its work is
in the totals, and what it ran is unknown.

**Requested and observed** are two `contracts.Settings`, and neither is filled from the other.
The observed one holds what the telemetry showed, where every host showed the same: the number
of hosts, the instance type, the image digest and the model revision, and the effective settings
the engine itself exposes (`EFFECTIVE`: prefix caching and the cache's data type, from the labels
of the engine's cache information metric, said through the window). The tokenizer's revision and
the gateway's profile are in no series: they are observed only when the driver hands in what it
read of them (`Driver`). A setting no evidence shows stays `None`, also when the launch
fingerprint matches, because a fingerprint cannot be read back into settings. The fingerprints
themselves are kept: each host's (`Host.launch_fingerprints`), and the pool's beside the
requested one.

**Each treatment** is one part of the requested settings, and is verified only when the
telemetry shows that part served the whole window on every host (`Treatment`). For the engine's
arguments the evidence is the launch fingerprint, `settings_fingerprint` of
`rhi_lifecycle_launch_info`: every host says one fingerprint, with no hole, through its time in
service, the same on every host, and it is the one requested. Two hosts of one image and one
model revision with two fingerprints are a verified release and unverified launch settings. A
treatment that is not verified has one `Reason`, and its observations are kept. A part the
request does not declare is unverified too, as `not_declared`: nothing holds the pool to it.

**Each host's metrics**, from the window's start through the drain (`contracts.HostMetric`): the
increase of the output, input, cached input and computed input token counters and of the
preemptions; the mean and the peak of the running and waiting requests and of the GPU
utilization. The computed input is the engine's own count of it, where its row has one
(`Further.COMPUTED_INPUT_TOKENS`): input less cached input is not it. A counter that restarted
is counted across the restart, and the restart is recorded. A counter with no sample before the
start counts the rises its samples show and nothing for its first value, which is no work of the
window. A series the workspace gave nothing of reads `None`, never zero, with the coverage
`missing`; one that does not cover the window reads what its samples give, with the coverage
`incomplete`. A gauge of several series (an engine index each) is read at the scrapes every one
of them reported, and is incomplete as soon as one of them is. The pool's total of a counter is
the sum over the hosts that reported it.

**The node's own liveness completion** (`rhi_lifecycle.serving`) goes to the engine and not
through the gateway. Its tokens are engine work like any other and stay in the totals and the
rates; nothing flags them.

**Other traffic** is read at the gateway, in aggregate: the increase of
`rhi_gateway_student_requests_total` over every identity of the pool, and over the canary
cohort's. An identity's series begins with its first request, so one that begins in the window
counts from zero. What the pool counted beyond the cohort came from someone else, a student or a
deployment's smoke test, and is `external_traffic`. What the cohort counted must be what the
driver says reached the gateway: its attempts, retries included, and the side queries of its
harnesses; a difference is `unreconciled_counters`. Matching counts say nothing of seconds the
gateway's counters do not cover: each of its tasks must have been scraped through the whole
window, and each series through the drain once it began, or the window lacks evidence
(`gateway_coverage_incomplete`). The same margins hold for everyone: a request of someone else
between the scrapes that bound the window counts as overlapping.

**Setup probes** the driver sent well before the window are in the scrape the increase runs
from, and in no count. One sent after that scrape is in the increase, and no counter tells it
from a request of the window. So the driver says how many it sent as the cohort from a time on
(`Driver.setup_probes`, `Driver.setup_from`), and that many are taken out of the cohort's count
and of the pool's, whatever the driver expected the counts to be. They are placed when every
series of the cohort was last scraped before that time: the increase then holds them all. With
no time, or a scrape after it, nothing shows how many the increase holds, and the reading says
so (`setup_probes_unplaced`) instead of reconciling.

**Warm-up, cache and preflight.** For each host: the output tokens its engine counted during the
warm-up, by the same rule as the window's; whether its required series cover the warm-up; and
whether the host is so shown to have worked, beyond what its liveness completions alone would
give. The cache capacity its lifecycle agent reported, the cache usage at the window's start and
its peak, and whether those series cover the window. The `Preflight` the driver took before the
run is kept with the reading.

**A window that cannot qualify** has `Reading.reasons`, each of which stands for one
`contracts.InvalidFlag` (`FLAG_OF`). The reading keeps every observation all the same.

**Nothing private leaves.** An instance's ID becomes the host's ordinal (`Ordinals`, which a run
keeps across its cells), and aliases and task IDs select series and are dropped. A label's value
reaches a reading only as a label of an enumeration or as a digest, a revision or a fingerprint
held to its grammar; anything else reads as not observed.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from statistics import fmean
from typing import TYPE_CHECKING, Any

from evaluate.contracts import INSTANCE_GPUS, HostMetric, InstanceType, InvalidFlag, KvDtype, Settings
from rhi_common.engine_metrics import Further, Kind, Quantity, cache_capacity, further_names, metric_names
from rhi_dashboards.gpu import COMPUTE as GPU_UTILIZATION_METRIC
from rhi_gateway.metrics import REQUESTS as GATEWAY_REQUESTS_METRIC
from rhi_lifecycle.cache import METRIC as CACHE_CAPACITY_METRIC
from rhi_lifecycle.serving import LAUNCH_METRIC, PROBE_INTERVAL_SECONDS, RELEASE_METRIC
from s5_capacity import (
    LOOKBACK_SECONDS,
    SCRAPE_SECONDS,
    SETTLE_SECONDS,
    Reader,
    counted_of,
    range_of,
    seconds_covered,
    selector,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from common import Series
    from rhi_common.engine_metrics import Name

# What the edges of a window are allowed: a scrape, and another for its delivery.
ALLOWANCE_SECONDS = 2 * SCRAPE_SECONDS

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


class Source(StrEnum):
    """A series the reader asks for beside the hosts' metrics (`contracts.HostMetric`)."""

    RELEASE = "release"
    LAUNCH = "launch"
    CACHE_CAPACITY = "cache_capacity"
    CACHE_CONFIG = "cache_config"
    CACHE_USAGE = "cache_usage"
    GATEWAY_REQUESTS = "gateway_requests"


type Key = Source | HostMetric
type Answers = Mapping[Key, Sequence[Series]]
type Sample = tuple[float, float]

COUNTERS = (
    HostMetric.OUTPUT_TOKENS_TOTAL,
    HostMetric.INPUT_TOKENS_TOTAL,
    HostMetric.CACHED_INPUT_TOKENS_TOTAL,
    HostMetric.COMPUTED_INPUT_TOKENS_TOTAL,
    HostMetric.PREEMPTIONS_TOTAL,
)
# How the series of one gauge on one host, read at one scrape, become one number.
GAUGES: dict[HostMetric, Callable[[list[float]], float]] = {
    HostMetric.RUNNING_REQUESTS: sum,
    HostMetric.WAITING_REQUESTS: sum,
    HostMetric.GPU_UTILIZATION: fmean,
}
# What every host of a cell must report: the contract's quantities. The preemptions are required
# too, under a reason of their own. The computed input and the GPU utilization are read where
# they exist: not every engine counts the first, and a host without a GPU has no exporter.
REQUIRED = (
    HostMetric.OUTPUT_TOKENS_TOTAL,
    HostMetric.INPUT_TOKENS_TOTAL,
    HostMetric.CACHED_INPUT_TOKENS_TOTAL,
    HostMetric.RUNNING_REQUESTS,
    HostMetric.WAITING_REQUESTS,
)
# The effective settings an engine exposes in its cache information metric (the metric its row
# names for the cache capacity): for each setting of `contracts.Settings`, the label that says
# it and what each of the label's texts means. The labels are vLLM's, the attributes of its
# `CacheConfig` written as text; `auto` is the model's own data type, the 16 bits
# (`rhi.pools.vllm_command`). A text outside the table, or an engine without the label, reads as
# not observed.
EFFECTIVE: dict[str, tuple[str, dict[str, object]]] = {
    "prefix_caching": ("enable_prefix_caching", {"True": True, "False": False}),
    "kv_dtype": ("cache_dtype", {"fp8": KvDtype.BITS_8, "auto": KvDtype.BITS_16}),
}


class Treatment(StrEnum):
    """A part of the requested settings that telemetry can establish, or fail to."""

    HOSTS = "hosts"
    """The number of hosts and their instance type, held through the window."""
    RELEASE = "release"
    """The engine image digest and the model revision."""
    LAUNCH_SETTINGS = "launch_settings"
    """The engine's arguments, by the fingerprint of those each host's engine was started with."""
    TOKENIZER = "tokenizer"
    GATEWAY_PROFILE = "gateway_profile"
    """The request side: the gateway's profile for the pool."""


class Reason(StrEnum):
    """Why a treatment is unverified, or why a window cannot qualify. Nothing else explains
    either: no text of a label, of the workspace or of AWS."""

    NOT_DECLARED = "not_declared"
    """The request names nothing for this part, so there is nothing to hold the pool to."""
    NOT_OBSERVED = "not_observed"
    """No evidence: no host, no series, no provenance, or a value outside its grammar."""
    INCOMPLETE_COVERAGE = "incomplete_coverage"
    """The evidence does not cover the window: a hole, or an edge with no sample."""
    MIXED = "mixed"
    """The hosts did not all report the same, or one reported two."""
    UNEXPECTED = "unexpected"
    """What every host reported is not what was requested."""
    HOST_TURNOVER = "host_turnover"
    """A host was in service for a part of the window only."""
    ENGINE_SERIES_MISSING = "engine_series_missing"
    PREEMPTION_COVERAGE_MISSING = "preemption_coverage_missing"
    WARM_UP_INCOMPLETE = "warm_up_incomplete"
    """Not every host was shown to work during a warm-up its required series cover."""
    CACHE_EVIDENCE_MISSING = "cache_evidence_missing"
    GATEWAY_COUNTERS_MISSING = "gateway_counters_missing"
    GATEWAY_COVERAGE_INCOMPLETE = "gateway_coverage_incomplete"
    """The gateway's counters do not cover the window: what passed in the rest is unknown."""
    EXTERNAL_TRAFFIC = "external_traffic"
    SETUP_PROBES_UNPLACED = "setup_probes_unplaced"
    """Nothing shows how many of the driver's setup probes the window's increase holds."""
    UNRECONCILED_COUNTERS = "unreconciled_counters"


# The flag each reason of a window stands for. The other reasons explain treatments only.
FLAG_OF = {
    Reason.NOT_OBSERVED: InvalidFlag.MISSING_EVIDENCE,
    Reason.INCOMPLETE_COVERAGE: InvalidFlag.MISSING_EVIDENCE,
    Reason.ENGINE_SERIES_MISSING: InvalidFlag.MISSING_EVIDENCE,
    Reason.PREEMPTION_COVERAGE_MISSING: InvalidFlag.MISSING_EVIDENCE,
    Reason.WARM_UP_INCOMPLETE: InvalidFlag.MISSING_EVIDENCE,
    Reason.CACHE_EVIDENCE_MISSING: InvalidFlag.MISSING_EVIDENCE,
    Reason.GATEWAY_COUNTERS_MISSING: InvalidFlag.MISSING_EVIDENCE,
    Reason.GATEWAY_COVERAGE_INCOMPLETE: InvalidFlag.MISSING_EVIDENCE,
    Reason.HOST_TURNOVER: InvalidFlag.HOST_TURNOVER,
    Reason.EXTERNAL_TRAFFIC: InvalidFlag.EXTERNAL_TRAFFIC,
    Reason.SETUP_PROBES_UNPLACED: InvalidFlag.UNRECONCILED_COUNTERS,
    Reason.UNRECONCILED_COUNTERS: InvalidFlag.UNRECONCILED_COUNTERS,
}


class Coverage(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MISSING = "missing"


_WORST_FIRST = (Coverage.MISSING, Coverage.INCOMPLETE, Coverage.COMPLETE)


def _worst(found: Iterable[Coverage]) -> Coverage:
    return min(found, key=_WORST_FIRST.index)


# --- What the reader is given ----------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """From `started` through `drained`, the end of the drain, in seconds since the epoch."""

    started: float
    drained: float

    def __post_init__(self) -> None:
        if not self.drained > self.started:
            raise ValueError("a window ends after it starts")

    @property
    def seconds(self) -> float:
        """The seconds that elapsed: what every rate of the window is over."""
        return self.drained - self.started


@dataclass(frozen=True)
class Requested:
    """The treatment: the requested settings, and the fingerprint of the engine arguments the
    requested release starts an engine with (`rhi_common.launch_settings.fingerprint`)."""

    settings: Settings
    launch_fingerprint: str | None = None


@dataclass(frozen=True)
class Preflight:
    """What `preflight` found before a run: the hosts in service, and what a host among them, or
    the gateway, did not report."""

    hosts: int
    missing: tuple[Key, ...]


@dataclass(frozen=True)
class Driver:
    """What the driver knows and no series says. `cohort` holds the gateway's aliases of the
    canary students the driver sends as: they select series and are never written. `attempts`
    and `side_queries` are the requests of the window that reached the gateway, retries among the
    first. `setup_probes` are the probes the driver sent as the cohort before the window from
    `setup_from` on, a time in seconds since the epoch: the ones a scrape before the window may
    not hold. A driver that sent its last probe more than `ALLOWANCE_SECONDS` before the window
    has none to name. The two provenances are what the driver read of the tokenizer and of the
    gateway's profile, `None` for nothing."""

    cohort: tuple[str, ...]
    attempts: int
    side_queries: int = 0
    setup_probes: int = 0
    setup_from: float | None = None
    warm_up: Window | None = None
    preflight: Preflight | None = None
    tokenizer_revision: str | None = None
    gateway_profile_digest: str | None = None


class Ordinals:
    """The local ordinal of each private label, in the order they were first seen. One of a run
    keeps a host's ordinal the same in every cell."""

    def __init__(self) -> None:
        self._known: dict[str, int] = {}

    def of(self, label: str) -> int:
        return self._known.setdefault(label, len(self._known))


# --- What a reading holds ----------------------------------------------------------------------


@dataclass(frozen=True)
class Counted:
    """A counter of one host over the window: its increase, `None` for no sample in the window,
    and the restarts from zero the increase was counted across."""

    increase: float | None
    resets: int
    coverage: Coverage


@dataclass(frozen=True)
class Level:
    """A gauge of one host over the window, at the scrapes every one of its series reported."""

    mean: float | None
    peak: float | None
    coverage: Coverage


@dataclass(frozen=True)
class Cache:
    """The cache evidence of one host, `None` for what was not reported: the one capacity its
    lifecycle agent said in the window, the usage at the window's start and its peak, and whether
    the capacity's and the usage's series cover the window."""

    capacity_tokens: float | None
    usage_at_start: float | None
    usage_peak: float | None
    coverage: Coverage


@dataclass(frozen=True)
class Host:
    """One host of the window, by its ordinal."""

    host: int
    instance_type: InstanceType | None
    image_digest: str | None
    model_revision: str | None
    """What its lifecycle agent said it serves, where it said one thing of the grammar."""
    launch_fingerprints: tuple[str, ...]
    """Every fingerprint its agent said in the window: one, on a host that kept its engine."""
    in_service_seconds: float
    membership: Reason | None
    """`None` for a host its release samples show through the whole window, or why they do not:
    `host_turnover`, `incomplete_coverage` for a hole, `not_observed` for no release sample."""
    counters: dict[HostMetric, Counted]
    gauges: dict[HostMetric, Level]
    output_tokens_per_s: float | None
    warm_up_output_tokens: float | None
    """What the engine counted during the warm-up; `None` with no warm-up or no sample in it."""
    warm_up_coverage: Coverage
    """Whether the host's required series and its preemptions cover the warm-up."""
    warmed: bool
    """Whether they do, and the tokens are more than the host's own liveness completions give."""
    cache: Cache

    @property
    def whole_window(self) -> bool:
        return self.membership is None


@dataclass(frozen=True)
class Totals:
    """The pool's totals: each counter summed over the hosts that reported it, `None` when none
    did, and the output rate over the window's seconds."""

    counters: Mapping[HostMetric, float | None]
    output_tokens_per_s: float | None


@dataclass(frozen=True)
class Traffic:
    """The gateway's requests over the window. `pool_requests` and `cohort_requests` are every
    identity's count and the canary cohort's, each less the setup probes taken out of it;
    `external_requests` is what the pool counted beyond the cohort. All three are `None` when the
    gateway's counters gave nothing. `setup_probes_placed` is whether the scrapes show the
    increase holds the probes taken out, and `coverage` whether the counters cover the window."""

    pool_requests: float | None
    cohort_requests: float | None
    external_requests: float | None
    driver_requests: int
    setup_probes_excluded: int
    setup_probes_placed: bool
    coverage: Coverage

    @property
    def matched(self) -> bool:
        """Whether the cohort's count is the number the driver says reached the gateway."""
        return self.cohort_requests is not None and round(self.cohort_requests) == self.driver_requests

    @property
    def reconciled(self) -> bool:
        """Whether that is established: the counts match, over counters that cover the window,
        with every setup probe placed."""
        return self.matched and self.setup_probes_placed and self.coverage is Coverage.COMPLETE


@dataclass(frozen=True)
class Reading:
    """What the telemetry says of one window."""

    seconds: float
    requested: Settings
    observed: Settings
    requested_launch_fingerprint: str | None
    observed_launch_fingerprint: str | None
    """The one fingerprint every host said, `None` when they said none or not the same."""
    treatments: Mapping[Treatment, Reason | None]
    """Each treatment's reason for being unverified, `None` for a verified one."""
    reasons: tuple[Reason, ...]
    """Why the window cannot qualify; empty when it can."""
    hosts: tuple[Host, ...]
    pool: Totals
    traffic: Traffic
    preflight: Preflight | None = field(default=None)

    @property
    def unverified(self) -> tuple[Treatment, ...]:
        return tuple(treatment for treatment, reason in self.treatments.items() if reason is not None)

    @property
    def flags(self) -> tuple[InvalidFlag, ...]:
        """The flags of the window's cell record, once each and in order."""
        return tuple(sorted({FLAG_OF[reason] for reason in self.reasons}))


# --- Asking the workspace ----------------------------------------------------------------------


def selectors(pool: str, engine: str) -> dict[Key, str]:
    """The series behind each key on the pool, by the names of `engine`'s row. A quantity the
    row leaves out has no selector, and reads as missing."""
    names, further, place = metric_names(engine), further_names(engine), cache_capacity(engine)

    def of(name: Name) -> str:
        return selector(name, "_total" if name.kind is Kind.COUNTER else "", pool)

    def plain(metric: str) -> str:
        return f'{metric}{{pool="{pool}"}}'

    found: dict[Key, str] = {
        Source.RELEASE: plain(RELEASE_METRIC),
        Source.LAUNCH: plain(LAUNCH_METRIC),
        Source.CACHE_CAPACITY: plain(CACHE_CAPACITY_METRIC),
        Source.CACHE_USAGE: of(names[Quantity.CACHE_USAGE]),
        Source.GATEWAY_REQUESTS: plain(f"{GATEWAY_REQUESTS_METRIC}_total"),
        HostMetric.OUTPUT_TOKENS_TOTAL: of(names[Quantity.OUTPUT_TOKENS]),
        HostMetric.INPUT_TOKENS_TOTAL: of(names[Quantity.INPUT_TOKENS]),
        HostMetric.CACHED_INPUT_TOKENS_TOTAL: of(names[Quantity.CACHED_INPUT_TOKENS]),
        HostMetric.RUNNING_REQUESTS: of(names[Quantity.RUNNING_REQUESTS]),
        HostMetric.WAITING_REQUESTS: of(names[Quantity.WAITING_REQUESTS]),
        HostMetric.GPU_UTILIZATION: plain(GPU_UTILIZATION_METRIC),
    }
    if place is not None:
        found[Source.CACHE_CONFIG] = of(place.metric)
    optional = {
        HostMetric.COMPUTED_INPUT_TOKENS_TOTAL: Further.COMPUTED_INPUT_TOKENS,
        HostMetric.PREEMPTIONS_TOTAL: Further.PREEMPTIONS,
    }
    return found | {key: of(further[quantity]) for key, quantity in optional.items() if quantity in further}


def readable_from(window: Window) -> float:
    """When the workspace holds the window's last scrapes: S5's settling time after the drain."""
    return window.drained + SETTLE_SECONDS


def read(
    reader: Reader, pool: str, engine: str, window: Window, warm_up: Window | None = None
) -> dict[Key, list[Series]]:
    """The workspace's samples of every series of `selectors`, as scraped, from the lookback
    before the window (and before `warm_up`) to `ALLOWANCE_SECONDS` after the drain."""
    low = min(window.started, warm_up.started) if warm_up else window.started
    after = window.drained + ALLOWANCE_SECONDS
    span = range_of(after - low + LOOKBACK_SECONDS)
    return {key: reader.read_samples(f"{held}[{span}]", at=after) for key, held in selectors(pool, engine).items()}


_PREFLIGHT = (
    Source.RELEASE,
    Source.LAUNCH,
    Source.CACHE_CAPACITY,
    Source.CACHE_USAGE,
    *REQUIRED,
    HostMetric.PREEMPTIONS_TOTAL,
)


def preflight(reader: Reader, pool: str, engine: str, at: float) -> Preflight:
    """Whether the evidence a reading needs can be read at `at`, before anything is measured: the
    hosts that report a release, and each key that one of them does not report (a fingerprint
    that is none counts as not reported), or that the gateway does not."""
    held, span = selectors(pool, engine), range_of(ALLOWANCE_SECONDS)

    def now(key: Key) -> list[Series]:
        return reader.read_samples(f"{held[key]}[{span}]", at=at) if key in held else []

    hosts = set(_by_host(now(Source.RELEASE)))
    missing: list[Key] = []
    for key in _PREFLIGHT:
        series = now(key)
        if key is Source.LAUNCH:
            series = [one for one in series if _FINGERPRINT.fullmatch(one.labels.get("settings_fingerprint", ""))]
        if not hosts or hosts - set(_by_host(series)):
            missing.append(key)
    if not now(Source.GATEWAY_REQUESTS):
        missing.append(Source.GATEWAY_REQUESTS)
    return Preflight(hosts=len(hosts), missing=tuple(missing))


# --- What a series says of a window ----------------------------------------------------------


def _by_host(series: Iterable[Series]) -> dict[str, list[Series]]:
    found: dict[str, list[Series]] = {}
    for one in series:
        if instance := one.labels.get("instance", ""):
            found.setdefault(instance, []).append(one)
    return found


def _scraped(one: Series) -> list[Sample]:
    """The series' samples in the order of their times, a sample that is no number left out."""
    return sorted((at, value) for at, value in one.samples if not math.isnan(value))


def _bounding(one: Series, window: Window) -> list[Sample]:
    """The samples that bound the window: from the last at or before its start to the first at
    or after its drain. What was scraped beyond them is not the window's."""
    scraped = _scraped(one)
    before = [sample for sample in scraped if sample[0] <= window.started]
    inside = [sample for sample in scraped if window.started < sample[0] < window.drained]
    after = [sample for sample in scraped if sample[0] >= window.drained]
    return [*before[-1:], *inside, *after[:1]]


def _unbroken(times: Sequence[float]) -> bool:
    return all(after - before <= ALLOWANCE_SECONDS for before, after in pairwise(times))


def _covers(times: Sequence[float], window: Window) -> bool:
    """Whether bounding samples at `times`, in order, cover the window: one at or before its
    start, one at or after its drain, and no two further apart than the allowance."""
    return bool(times) and times[0] <= window.started and times[-1] >= window.drained and _unbroken(times)


def _shown(times: Iterable[float], low: float, high: float) -> Reason | None:
    """Whether an agent's samples at `times` show what they say held from `low` to `high`: `None`
    when they start and end within the allowance of the two with no hole between. Else why not:
    no sample, a part of the time only, or a hole."""
    held = sorted({at for at in times if low - ALLOWANCE_SECONDS <= at <= high + ALLOWANCE_SECONDS})
    if not held:
        return Reason.NOT_OBSERVED
    if held[0] > low + ALLOWANCE_SECONDS or held[-1] < high - ALLOWANCE_SECONDS:
        return Reason.HOST_TURNOVER
    return None if _unbroken(held) else Reason.INCOMPLETE_COVERAGE


def _risen(one: Series, window: Window, *, from_zero: bool) -> tuple[float, int, list[float]] | None:
    """One counter series over the window: its increase, its restarts and the times of the
    samples that bound the window. `None` for a series that is not the window's: its last sample
    is more than the allowance before the start. One scraped just before the start and never
    after stopped with the window, and counts nothing over times that do not cover it. A series
    with no sample before the start counts from zero when `from_zero`, as one that began with
    its first event; else from its first sample, whose value shows no work of the window."""
    held = _bounding(one, window)
    if not held or held[-1][0] < window.started - ALLOWANCE_SECONDS:
        return None
    start = window.started if from_zero or held[0][0] <= window.started else held[0][0]
    # S5's increase is by instance: the series is given one name here, whatever labels it has.
    alone = dataclasses.replace(one, labels={"instance": "one"}, samples=tuple(held))
    increase = sum(counted_of([alone], start).values())
    resets = sum(after < before for (_, before), (_, after) in pairwise(held))
    return increase, resets, [at for at, _ in held]


def _counted(series: Sequence[Series], window: Window) -> Counted:
    """One counter of one host over the window, from its series."""
    risen = [found for one in series if (found := _risen(one, window, from_zero=False)) is not None]
    if not any(times[-1] > window.started for _, _, times in risen):
        return Counted(None, 0, Coverage.MISSING)
    complete = all(_covers(times, window) for _, _, times in risen)
    return Counted(
        sum(increase for increase, _, _ in risen),
        sum(resets for _, resets, _ in risen),
        Coverage.COMPLETE if complete else Coverage.INCOMPLETE,
    )


def _gauge(
    series: Sequence[Series], window: Window, combine: Callable[[list[float]], float]
) -> tuple[list[Sample], Coverage]:
    """One gauge of one host: one number at each scrape every one of its series reported, from
    the samples that bound the window, and whether each of those series covers it. A series with
    no sample in the window is not one of them; one that stopped within the allowance before the
    window still makes the gauge incomplete."""
    near = [dict(_bounding(one, window)) for one in series]
    near = [one for one in near if one and max(one) >= window.started - ALLOWANCE_SECONDS]
    held = [one for one in near if any(window.started <= at <= window.drained for at in one)]
    if not held:
        return [], Coverage.MISSING
    complete = all(_covers(sorted(one), window) for one in near)
    times = sorted(set.intersection(*(set(one) for one in held)))
    scrapes = [(at, combine([one[at] for one in held])) for at in times]
    return scrapes, Coverage.COMPLETE if complete else Coverage.INCOMPLETE


def _level(series: Sequence[Series], window: Window, combine: Callable[[list[float]], float]) -> Level:
    """One gauge of one host over the window, from its series."""
    scrapes, coverage = _gauge(series, window, combine)
    inside = [value for at, value in scrapes if window.started <= at <= window.drained]
    return Level(fmean(inside) if inside else None, max(inside, default=None), coverage)


def _cache(capacity: Sequence[Series], usage: Sequence[Series], window: Window) -> Cache:
    said, capacity_coverage = _gauge(capacity, window, max)
    capacities = {value for at, value in said if window.started <= at <= window.drained}
    scrapes, usage_coverage = _gauge(usage, window, max)
    at_start = [value for at, value in scrapes if window.started - ALLOWANCE_SECONDS <= at <= window.started]
    return Cache(
        capacity_tokens=next(iter(capacities)) if len(capacities) == 1 else None,
        usage_at_start=at_start[-1] if at_start else None,
        usage_peak=max((value for at, value in scrapes if window.started <= at <= window.drained), default=None),
        coverage=_worst((capacity_coverage, usage_coverage)),
    )


def _times(series: Iterable[Series], labels: Sequence[str]) -> dict[tuple[str, ...], list[float]]:
    """The times of one host's samples, by the values of `labels` they carry."""
    found: dict[tuple[str, ...], list[float]] = {}
    for one in series:
        said = tuple(one.labels.get(label, "") for label in labels)
        found.setdefault(said, []).extend(at for at, _ in _scraped(one))
    return found


def _said(by: Mapping[tuple[str, ...], list[float]], window: Window) -> dict[tuple[str, ...], float]:
    """The seconds of the window each value was reported for, those reported for none left out."""
    seconds = {said: seconds_covered(taken, window.started, window.drained) for said, taken in by.items()}
    return {said: held for said, held in seconds.items() if held > 0}


def _only[T](values: Iterable[T | None]) -> T | None:
    """The one thing all of `values` are, when there is a value, none is `None` and all agree."""
    held = list(values)
    return held[0] if held and all(one is not None and one == held[0] for one in held) else None


def _held(value: str | None, grammar: re.Pattern[str]) -> str | None:
    return value if value is not None and grammar.fullmatch(value) else None


def _effective(series: Sequence[Series], window: Window) -> dict[str, object]:
    """What one host's engine said of each setting of `EFFECTIVE`: its meaning where every
    series of the host that reported in the window said the same text, through the window."""
    reported = [one for one in series if any(window.started <= at <= window.drained for at, _ in _scraped(one))]
    whole = _shown((at for one in reported for at, _ in _scraped(one)), window.started, window.drained) is None
    found: dict[str, object] = {}
    for setting, (label, meanings) in EFFECTIVE.items():
        text = _only(one.labels.get(label) for one in reported)
        found[setting] = meanings.get(text) if whole and text is not None else None
    return found


def _match(declared: Sequence[object], observed: Sequence[object]) -> Reason | None:
    """Whether what was observed is what was declared, part by part, for the parts declared."""
    pairs = [(asked, seen) for asked, seen in zip(declared, observed, strict=True) if asked is not None]
    if not pairs:
        return Reason.NOT_DECLARED
    if any(seen is None for _, seen in pairs):
        return Reason.NOT_OBSERVED
    return None if all(asked == seen for asked, seen in pairs) else Reason.UNEXPECTED


def _launch(requested: str | None, hosts: Sequence[tuple[tuple[str, ...], bool]]) -> Reason | None:
    """The launch settings' reason, from each host's fingerprints in the window and whether one
    fingerprint, and nothing else, was said through the host's time in service."""
    if _held(requested, _FINGERPRINT) is None:
        return Reason.NOT_DECLARED
    fingerprints = {one for said, _ in hosts for one in said}
    if len(fingerprints) > 1:
        return Reason.MIXED
    if not hosts or not all(said for said, _ in hosts):
        return Reason.NOT_OBSERVED
    if not all(whole for _, whole in hosts):
        return Reason.INCOMPLETE_COVERAGE
    return None if fingerprints == {requested} else Reason.UNEXPECTED


def _traffic(series: Sequence[Series], window: Window, driver: Driver) -> Traffic:
    expected = driver.attempts + driver.side_queries
    risen = [(one, found) for one in series if (found := _risen(one, window, from_zero=True)) is not None]
    if not any(times[-1] > window.started for _, (_, _, times) in risen):
        return Traffic(None, None, None, expected, 0, driver.setup_probes == 0, Coverage.MISSING)
    # Each task of the gateway must show scrapes through the whole window, by any of its series:
    # only then did a series that begins in the window begin with its first request. And each
    # series, once it began, must go on to the drain.
    tasks: dict[str, set[float]] = {}
    for one, (_, _, times) in risen:
        tasks.setdefault(one.labels.get("instance", ""), set()).update(times)
    complete = all(_covers(sorted(times), window) for times in tasks.values()) and all(
        times[-1] >= window.drained and _unbroken(times) for _, (_, _, times) in risen
    )
    pool = sum(increase for _, (increase, _, _) in risen)
    cohort = [found for one, found in risen if one.labels.get("alias", "") in driver.cohort]
    counted = sum(increase for increase, _, _ in cohort)
    # A probe is in the increase when it was sent after the scrape the increase runs from. A
    # series that begins in the window holds every request of its identity.
    probes = min(driver.setup_probes, max(round(counted), 0))
    since = driver.setup_from
    placed = driver.setup_probes == 0 or (
        probes == driver.setup_probes
        and since is not None
        and all(times[0] > window.started or times[0] < since for _, _, times in cohort)
    )
    return Traffic(
        pool_requests=pool - probes,
        cohort_requests=counted - probes,
        external_requests=pool - counted,
        driver_requests=expected,
        setup_probes_excluded=probes,
        setup_probes_placed=placed,
        coverage=Coverage.COMPLETE if complete else Coverage.INCOMPLETE,
    )


def _reasons(hosts: Sequence[Host], traffic: Traffic) -> tuple[Reason, ...]:
    """Why a window of these hosts and this traffic cannot qualify, each reason once, in order."""
    reasons: set[Reason] = set()
    if not hosts:
        reasons.add(Reason.NOT_OBSERVED)
    for host in hosts:
        if host.membership is not None:
            reasons.add(host.membership)
            if host.membership is not Reason.INCOMPLETE_COVERAGE:
                # A host of a part of the window, or of no release, does not owe the whole
                # evidence; the others do, and so does one whose release samples have a hole.
                continue
        preemptions = host.counters[HostMetric.PREEMPTIONS_TOTAL].coverage
        readings: dict[HostMetric, Counted | Level] = {**host.counters, **host.gauges}
        covered = [readings[metric].coverage for metric in REQUIRED]
        if Coverage.MISSING in covered:
            reasons.add(Reason.ENGINE_SERIES_MISSING)
        if preemptions is Coverage.MISSING:
            reasons.add(Reason.PREEMPTION_COVERAGE_MISSING)
        if Coverage.INCOMPLETE in (*covered, preemptions, host.cache.coverage):
            reasons.add(Reason.INCOMPLETE_COVERAGE)
        if not host.warmed:
            reasons.add(Reason.WARM_UP_INCOMPLETE)
        if host.cache.capacity_tokens is None or host.cache.usage_at_start is None:
            reasons.add(Reason.CACHE_EVIDENCE_MISSING)
    if traffic.coverage is Coverage.MISSING:
        reasons.add(Reason.GATEWAY_COUNTERS_MISSING)
    else:
        if traffic.coverage is Coverage.INCOMPLETE:
            reasons.add(Reason.GATEWAY_COVERAGE_INCOMPLETE)
        if traffic.external_requests:
            reasons.add(Reason.EXTERNAL_TRAFFIC)
        if not traffic.setup_probes_placed:
            reasons.add(Reason.SETUP_PROBES_UNPLACED)
        if not traffic.matched:
            reasons.add(Reason.UNRECONCILED_COUNTERS)
    return tuple(sorted(reasons))


def _warm_up(of: Callable[[Key], list[Series]], warm_up: Window | None) -> tuple[float | None, Coverage, bool]:
    """What one host's engine counted during the warm-up, whether the host's required series and
    its preemptions cover it, and whether the host is so shown to have worked: more tokens than
    a liveness completion of one token each minute gives between the samples that bound it."""
    if warm_up is None:
        return None, Coverage.MISSING, False
    worked = _counted(of(HostMetric.OUTPUT_TOKENS_TOTAL), warm_up).increase
    coverage = _worst(
        _counted(of(metric), warm_up).coverage if metric in COUNTERS else _gauge(of(metric), warm_up, GAUGES[metric])[1]
        for metric in (*REQUIRED, HostMetric.PREEMPTIONS_TOTAL)
    )
    liveness = int((warm_up.seconds + 2 * ALLOWANCE_SECONDS) // PROBE_INTERVAL_SECONDS) + 1
    return worked, coverage, coverage is Coverage.COMPLETE and worked is not None and worked > liveness


def resolve(
    answers: Answers, window: Window, requested: Requested, driver: Driver, ordinals: Ordinals | None = None
) -> Reading:
    """What the answers of `read` say of the window: the observed settings beside the requested
    ones, each treatment verified or not, each host's metrics and the pool's totals, the gateway's
    traffic, and the reasons the window cannot qualify."""
    ordinals = Ordinals() if ordinals is None else ordinals
    by_key = {key: _by_host(series) for key, series in answers.items() if key is not Source.GATEWAY_REQUESTS}
    # A host is an instance that reported a release in the window, or anything else in it.
    instances = {
        instance
        for held in by_key.values()
        for instance, series in held.items()
        if any(window.started <= at <= window.drained for one in series for at, _ in _scraped(one))
    }

    hosts: list[Host] = []
    identities: set[tuple[str, str]] = set()
    launched: list[tuple[tuple[str, ...], bool]] = []
    effective: list[dict[str, object]] = []
    for instance in sorted(instances | set(by_key.get(Source.RELEASE, {}))):

        def of(key: Key, instance: str = instance) -> list[Series]:
            return by_key.get(key, {}).get(instance, [])

        releases = _times(of(Source.RELEASE), ("instance_type", "image_digest", "model_revision"))
        served = _said(releases, window)
        if not served and instance not in instances:
            continue
        taken = [at for times in releases.values() for at in times]
        membership = _shown(taken, window.started, window.drained) if served else Reason.NOT_OBSERVED
        identities |= {(image, revision) for _, image, revision in served}

        # The fingerprint must be said through the host's time in service: the window, or the
        # part of it the release samples of a host that joined or left show.
        low, high = window.started, window.drained
        if membership is Reason.HOST_TURNOVER:
            near = [at for at in taken if low - ALLOWANCE_SECONDS <= at <= high + ALLOWANCE_SECONDS]
            low, high = max(low, min(near)), min(high, max(near))
        said = _said(_times(of(Source.LAUNCH), ("settings_fingerprint",)), window)
        fingerprints = tuple(sorted(one for (one,) in said if _FINGERPRINT.fullmatch(one)))
        whole = (
            len(said) == 1
            and len(fingerprints) == 1
            and _shown((at for one in of(Source.LAUNCH) for at, _ in _scraped(one)), low, high) is None
        )
        launched.append((fingerprints, whole))
        effective.append(_effective(of(Source.CACHE_CONFIG), window))

        counters = {metric: _counted(of(metric), window) for metric in COUNTERS}
        output = counters[HostMetric.OUTPUT_TOKENS_TOTAL].increase
        worked, warm_up_coverage, warmed = _warm_up(of, driver.warm_up)
        kind = _only(kind for kind, _, _ in served)
        hosts.append(
            Host(
                host=ordinals.of(instance),
                instance_type=InstanceType(kind) if kind in set(InstanceType) else None,
                image_digest=_held(_only(image for _, image, _ in served), _DIGEST),
                model_revision=_held(_only(revision for _, _, revision in served), _COMMIT),
                launch_fingerprints=fingerprints,
                in_service_seconds=seconds_covered(taken, window.started, window.drained),
                membership=membership,
                counters=counters,
                gauges={metric: _level(of(metric), window, combine) for metric, combine in GAUGES.items()},
                output_tokens_per_s=None if output is None else output / window.seconds,
                warm_up_output_tokens=worked,
                warm_up_coverage=warm_up_coverage,
                warmed=warmed,
                cache=_cache(of(Source.CACHE_CAPACITY), of(Source.CACHE_USAGE), window),
            )
        )

    instance_type = _only(host.instance_type for host in hosts)
    gpu_type, gpus = INSTANCE_GPUS[instance_type] if instance_type is not None else (None, None)
    image = _only(host.image_digest for host in hosts)
    revision = _only(host.model_revision for host in hosts)
    observed: dict[str, Any] = dict.fromkeys(spec.name for spec in dataclasses.fields(Settings))
    observed |= {
        "host_count": len(hosts) or None,
        "instance_type": instance_type,
        "gpu_type": gpu_type,
        "gpus_per_host": gpus,
        "model_revision": revision,
        "engine_image_digest": image,
        "tokenizer_revision": _held(driver.tokenizer_revision, _COMMIT),
        "gateway_profile_digest": _held(driver.gateway_profile_digest, _DIGEST),
        **{setting: _only(said[setting] for said in effective) for setting in EFFECTIVE},
    }
    seen = Settings(**observed)
    asked = requested.settings

    # What stands in the way of a claim about every host through the window, the worst first.
    memberships = {host.membership for host in hosts}
    gap = next(
        (
            reason
            for reason in (Reason.HOST_TURNOVER, Reason.NOT_OBSERVED, Reason.INCOMPLETE_COVERAGE)
            if reason in memberships
        ),
        None,
    )
    if asked.host_count is None and asked.instance_type is None:
        membership = Reason.NOT_DECLARED
    elif not hosts:
        membership = Reason.NOT_OBSERVED
    elif gap is not None:
        membership = gap
    elif len({host.instance_type for host in hosts} - {None}) > 1:
        membership = Reason.MIXED
    else:
        membership = _match((asked.host_count, asked.instance_type), (seen.host_count, seen.instance_type))
    release = _match((asked.engine_image_digest, asked.model_revision), (image, revision))
    if release is not Reason.NOT_DECLARED:
        if len(identities) > 1:
            release = Reason.MIXED
        elif Reason.NOT_OBSERVED in memberships:
            release = Reason.NOT_OBSERVED
        elif Reason.INCOMPLETE_COVERAGE in memberships:
            release = Reason.INCOMPLETE_COVERAGE
    treatments = {
        Treatment.HOSTS: membership,
        Treatment.RELEASE: release,
        Treatment.LAUNCH_SETTINGS: _launch(requested.launch_fingerprint, launched),
        Treatment.TOKENIZER: _match((asked.tokenizer_revision,), (seen.tokenizer_revision,)),
        Treatment.GATEWAY_PROFILE: _match((asked.gateway_profile_digest,), (seen.gateway_profile_digest,)),
    }

    traffic = _traffic(answers.get(Source.GATEWAY_REQUESTS, ()), window, driver)
    totals: dict[HostMetric, float | None] = {}
    for metric in COUNTERS:
        reported = [host.counters[metric].increase for host in hosts]
        totals[metric] = (
            sum(one for one in reported if one is not None) if any(one is not None for one in reported) else None
        )
    output = totals[HostMetric.OUTPUT_TOKENS_TOTAL]
    return Reading(
        seconds=window.seconds,
        requested=asked,
        observed=seen,
        requested_launch_fingerprint=_held(requested.launch_fingerprint, _FINGERPRINT),
        observed_launch_fingerprint=_only(said[0] if len(said) == 1 else None for said, _ in launched),
        treatments=treatments,
        reasons=_reasons(hosts, traffic),
        hosts=tuple(sorted(hosts, key=lambda host: host.host)),
        pool=Totals(totals, None if output is None else output / window.seconds),
        traffic=traffic,
        preflight=driver.preflight,
    )
