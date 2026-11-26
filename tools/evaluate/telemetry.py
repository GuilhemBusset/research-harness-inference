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
`ALLOWANCE_SECONDS` after the drain: the scrape after the drain holds what the last answers
added, and it takes a moment to reach the workspace: read no sooner than `readable_from` says.

**The hosts** are the instances whose lifecycle agent reported a release
(`rhi_lifecycle_release_info`) for some seconds of the window. A host is in service within a
scrape of each of its samples, as in S5, and served the whole window when those seconds are the
window's to within `ALLOWANCE_SECONDS`, a scrape at each edge. A host that was in service for a
part of the window only joined or left during it: the cell had a turnover of hosts. So does a
host whose release samples have a hole, since nothing shows it stayed.

**Requested and observed** are two `contracts.Settings`, and neither is filled from the other.
The observed one holds what the telemetry showed: the number of hosts, and the instance type,
the image digest and the model revision where every host reported the same one. The tokenizer's
revision and the gateway's profile are in no series: they are observed only when the driver
hands in what it read of them (`Driver`). A setting no evidence shows stays `None`, also when
the launch fingerprint matches, because a fingerprint cannot be read back into settings.

**Each treatment** is one part of the requested settings, and is verified only when the
telemetry shows that part served the whole window on every host (`Treatment`). For the engine's
arguments the evidence is the launch fingerprint, `settings_fingerprint` of
`rhi_lifecycle_launch_info`: every host says one fingerprint through its time in service, the
same on every host, and it is the one requested. Two hosts of one image and one model revision
with two fingerprints are a verified release and unverified launch settings. A treatment that is
not verified has one `Reason`, and its observations are kept. A part the request does not declare
is unverified too, as `not_declared`: nothing holds the pool to it.

**Each host's metrics**, from the window's start through the drain (`contracts.HostMetric`): the
increase of the output, input, cached input and computed input token counters and of the
preemptions; the mean and the peak of the running and waiting requests and of the GPU
utilization. The computed input is the engine's own count of it, where its row has one
(`Further.COMPUTED_INPUT_TOKENS`): input less cached input is not it. A counter that restarted
is counted across the restart, and the restart is recorded. A series the workspace gave nothing
of reads `None`, never zero, with the coverage `missing`; one with a hole longer than
`ALLOWANCE_SECONDS`, no sample just before the window or none after the drain reads what its
samples give, with the coverage `incomplete`. The pool's total of a counter is the sum over the
hosts that reported it.

**The node's own liveness completion** (`rhi_lifecycle.serving`) goes to the engine and not
through the gateway. Its tokens are engine work like any other and stay in the totals and the
rates; nothing flags them.

**Other traffic** is read at the gateway, in aggregate: the increase of
`rhi_gateway_student_requests_total` over every identity of the pool, and over the canary
cohort's. What the pool counted beyond the cohort came from someone else, a student or a
deployment's smoke test, and is `external_traffic`. What the cohort counted must be what the
driver says reached the gateway: its attempts, retries included, and the side queries of its
harnesses. The increase runs from the last scrape before the window, so a setup probe the driver
sent after that scrape is in it: the driver says how many it sent within that margin, and up to
that many are taken out of the cohort's count before it is compared. Probes sent earlier are in
neither count. A difference that remains is `unreconciled_counters`. The same margins hold for
everyone: a request of someone else within a scrape of the window's edges counts as overlapping.

**Warm-up, cache and preflight.** For each host: the output tokens its engine counted during the
warm-up, and whether they are more than its liveness completions alone would give; the cache
capacity its lifecycle agent reported, the cache usage at the window's start and its peak. The
`Preflight` the driver took before the run is kept with the reading.

**A window that cannot qualify** has `Reading.reasons`, each of which stands for one
`contracts.InvalidFlag` (`FLAG_OF`). The reading keeps every observation all the same.

**Nothing private leaves.** An instance's ID becomes the host's ordinal (`Ordinals`, which a run
keeps across its cells), and aliases and task IDs select series and are dropped. A label's value
reaches a reading only as a label of an enumeration or as a digest or a revision held to its
grammar; anything else reads as not observed.
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

from evaluate.contracts import INSTANCE_GPUS, HostMetric, InstanceType, InvalidFlag, Settings
from rhi_common.engine_metrics import Further, Kind, Quantity, further_names, metric_names
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
    CACHE_USAGE = "cache_usage"
    GATEWAY_REQUESTS = "gateway_requests"


type Key = Source | HostMetric
type Answers = Mapping[Key, Sequence[Series]]

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
    """Not every host was shown to work during a warm-up."""
    CACHE_EVIDENCE_MISSING = "cache_evidence_missing"
    GATEWAY_COUNTERS_MISSING = "gateway_counters_missing"
    EXTERNAL_TRAFFIC = "external_traffic"
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
    Reason.HOST_TURNOVER: InvalidFlag.HOST_TURNOVER,
    Reason.EXTERNAL_TRAFFIC: InvalidFlag.EXTERNAL_TRAFFIC,
    Reason.UNRECONCILED_COUNTERS: InvalidFlag.UNRECONCILED_COUNTERS,
}


class Coverage(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MISSING = "missing"


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
    first. `setup_probes` are the probes the driver sent as the cohort within
    `ALLOWANCE_SECONDS` before the window, which the gateway's counters cannot tell from the
    window's requests. The two provenances are what the driver read of the tokenizer and of the
    gateway's profile, `None` for nothing."""

    cohort: tuple[str, ...]
    attempts: int
    side_queries: int = 0
    setup_probes: int = 0
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
    """A gauge of one host over the window."""

    mean: float | None
    peak: float | None
    coverage: Coverage


@dataclass(frozen=True)
class Cache:
    """The cache evidence of one host. `None` for what was not reported."""

    capacity_tokens: float | None
    usage_at_start: float | None
    usage_peak: float | None


@dataclass(frozen=True)
class Host:
    """One host of the window, by its ordinal."""

    host: int
    instance_type: InstanceType | None
    in_service_seconds: float
    whole_window: bool
    counters: dict[HostMetric, Counted]
    gauges: dict[HostMetric, Level]
    output_tokens_per_s: float | None
    warm_up_output_tokens: float | None
    """What the engine counted during the warm-up; `None` with no warm-up or no sample in it."""
    warmed: bool
    """Whether that is more than the host's own liveness completions would give."""
    cache: Cache


@dataclass(frozen=True)
class Totals:
    """The pool's totals: each counter summed over the hosts that reported it, `None` when none
    did, and the output rate over the window's seconds."""

    counters: Mapping[HostMetric, float | None]
    output_tokens_per_s: float | None


@dataclass(frozen=True)
class Traffic:
    """The gateway's requests over the window. `cohort_requests` is the canary cohort's count
    less the setup probes taken out of it; `external_requests` is what the pool counted beyond
    the cohort. All three are `None` when the gateway's counters gave nothing."""

    pool_requests: float | None
    cohort_requests: float | None
    external_requests: float | None
    driver_requests: int
    setup_probes_excluded: int

    @property
    def reconciled(self) -> bool:
        """Whether the cohort's count is what the driver says reached the gateway."""
        return self.cohort_requests is not None and round(self.cohort_requests) == self.driver_requests


@dataclass(frozen=True)
class Reading:
    """What the telemetry says of one window."""

    seconds: float
    requested: Settings
    observed: Settings
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
    names, further = metric_names(engine), further_names(engine)

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


# --- Reading the answers -----------------------------------------------------------------------


def _by_host(series: Iterable[Series]) -> dict[str, list[Series]]:
    found: dict[str, list[Series]] = {}
    for one in series:
        if instance := one.labels.get("instance", ""):
            found.setdefault(instance, []).append(one)
    return found


def _scraped(one: Series) -> list[tuple[float, float]]:
    """The series' samples in the order of their times, a sample that is no number left out."""
    return sorted((at, value) for at, value in one.samples if not math.isnan(value))


def _complete(times: Sequence[float], window: Window) -> bool:
    """Whether samples at `times`, in order, cover the window: one just before it, one after the
    drain, and no hole longer than the allowance between them."""
    held = [at for at in times if at >= window.started - ALLOWANCE_SECONDS]
    if not held or held[0] > window.started or held[-1] < window.drained:
        return False
    return all(after - before <= ALLOWANCE_SECONDS for before, after in pairwise(held))


def _counted(series: Sequence[Series], window: Window) -> Counted:
    """One counter of one host over the window, from its series."""
    increase = counted_of(series, window.started)
    if not increase:
        return Counted(None, 0, Coverage.MISSING)
    resets, complete = 0, True
    for one in series:
        scraped = _scraped(one)
        inside = [value for at, value in scraped if at > window.started]
        if not inside:
            continue
        before = [value for at, value in scraped if at <= window.started]
        resets += sum(after < held for held, after in pairwise([*before[-1:], *inside]))
        complete = complete and _complete([at for at, _ in scraped], window)
    return Counted(sum(increase.values()), resets, Coverage.COMPLETE if complete else Coverage.INCOMPLETE)


def _scrapes(series: Sequence[Series], combine: Callable[[list[float]], float]) -> list[tuple[float, float]]:
    """One number a scrape of one host's gauge: its series' values at each time, combined."""
    values: dict[float, list[float]] = {}
    for one in series:
        for at, value in _scraped(one):
            values.setdefault(at, []).append(value)
    return [(at, combine(held)) for at, held in sorted(values.items())]


def _level(series: Sequence[Series], window: Window, combine: Callable[[list[float]], float]) -> Level:
    """One gauge of one host over the window, from its series."""
    scrapes = _scrapes(series, combine)
    inside = [value for at, value in scrapes if window.started <= at <= window.drained]
    if not inside:
        return Level(None, None, Coverage.MISSING)
    complete = _complete([at for at, _ in scrapes], window)
    return Level(fmean(inside), max(inside), Coverage.COMPLETE if complete else Coverage.INCOMPLETE)


def _times(series: Iterable[Series], labels: Sequence[str]) -> dict[str, dict[tuple[str, ...], list[float]]]:
    """For each instance, the times of its samples by the values of `labels` they carry."""
    found: dict[str, dict[tuple[str, ...], list[float]]] = {}
    for instance, held in _by_host(series).items():
        for one in held:
            said = tuple(one.labels.get(label, "") for label in labels)
            found.setdefault(instance, {}).setdefault(said, []).extend(at for at, _ in one.samples)
    return found


def _said(by: Mapping[tuple[str, ...], list[float]], window: Window) -> dict[tuple[str, ...], float]:
    """The seconds of the window each value was reported for, those reported for none left out."""
    seconds = {said: seconds_covered(taken, window.started, window.drained) for said, taken in by.items()}
    return {said: held for said, held in seconds.items() if held > 0}


def _one(values: set[str], grammar: re.Pattern[str]) -> str | None:
    """The one value every host reported, when it is one and of its grammar."""
    return next(iter(values)) if len(values) == 1 and grammar.fullmatch(next(iter(values))) else None


def _held(value: str | None, grammar: re.Pattern[str]) -> str | None:
    return value if value is not None and grammar.fullmatch(value) else None


def _match(declared: Sequence[object], observed: Sequence[object]) -> Reason | None:
    """Whether what was observed is what was declared, part by part, for the parts declared."""
    pairs = [(asked, seen) for asked, seen in zip(declared, observed, strict=True) if asked is not None]
    if not pairs:
        return Reason.NOT_DECLARED
    if any(seen is None for _, seen in pairs):
        return Reason.NOT_OBSERVED
    return None if all(asked == seen for asked, seen in pairs) else Reason.UNEXPECTED


def _launch(requested: str | None, hosts: Sequence[tuple[set[str], bool]]) -> Reason | None:
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
    pool = counted_of(series, window.started)
    if not pool:
        return Traffic(None, None, None, expected, 0)
    cohort = sum(
        counted_of([one for one in series if one.labels.get("alias", "") in driver.cohort], window.started).values()
    )
    # A setup probe sent after the last scrape before the window is in the cohort's increase.
    probes = min(max(round(cohort) - expected, 0), driver.setup_probes)
    total = sum(pool.values())
    return Traffic(total, cohort - probes, total - cohort, expected, probes)


def _until(series: Sequence[Series], end: float) -> list[Series]:
    return [
        dataclasses.replace(one, samples=tuple(sample for sample in one.samples if sample[0] <= end)) for one in series
    ]


def _last(scrapes: Sequence[tuple[float, float]], low: float, high: float) -> float | None:
    inside = [value for at, value in scrapes if low <= at <= high]
    return inside[-1] if inside else None


def _reasons(hosts: Sequence[Host], traffic: Traffic) -> tuple[Reason, ...]:
    """Why a window of these hosts and this traffic cannot qualify, each reason once, in order."""
    reasons: set[Reason] = set()
    if not hosts:
        reasons.add(Reason.NOT_OBSERVED)
    for host in hosts:
        if not host.whole_window:
            # A host of a part of the window is the turnover's; the others owe the whole evidence.
            reasons.add(Reason.HOST_TURNOVER)
            continue
        preemptions = host.counters[HostMetric.PREEMPTIONS_TOTAL].coverage
        readings: dict[HostMetric, Counted | Level] = {**host.counters, **host.gauges}
        covered = [readings[metric].coverage for metric in REQUIRED]
        if Coverage.MISSING in covered:
            reasons.add(Reason.ENGINE_SERIES_MISSING)
        if preemptions is Coverage.MISSING:
            reasons.add(Reason.PREEMPTION_COVERAGE_MISSING)
        if Coverage.INCOMPLETE in (*covered, preemptions):
            reasons.add(Reason.INCOMPLETE_COVERAGE)
        if not host.warmed:
            reasons.add(Reason.WARM_UP_INCOMPLETE)
        if host.cache.capacity_tokens is None or host.cache.usage_at_start is None:
            reasons.add(Reason.CACHE_EVIDENCE_MISSING)
    if traffic.pool_requests is None:
        reasons.add(Reason.GATEWAY_COUNTERS_MISSING)
    else:
        if traffic.external_requests:
            reasons.add(Reason.EXTERNAL_TRAFFIC)
        if not traffic.reconciled:
            reasons.add(Reason.UNRECONCILED_COUNTERS)
    return tuple(sorted(reasons))


def resolve(
    answers: Answers, window: Window, requested: Requested, driver: Driver, ordinals: Ordinals | None = None
) -> Reading:
    """What the answers of `read` say of the window: the observed settings beside the requested
    ones, each treatment verified or not, each host's metrics and the pool's totals, the gateway's
    traffic, and the reasons the window cannot qualify."""
    ordinals = Ordinals() if ordinals is None else ordinals
    by_key = {key: _by_host(series) for key, series in answers.items()}
    releases = _times(answers.get(Source.RELEASE, ()), ("instance_type", "image_digest", "model_revision"))
    launches = _times(answers.get(Source.LAUNCH, ()), ("settings_fingerprint",))

    hosts: list[Host] = []
    identities: set[tuple[str, ...]] = set()
    launched: list[tuple[set[str], bool]] = []
    for instance in sorted(releases):
        served = _said(releases[instance], window)
        if not served:
            continue
        taken = [at for times in releases[instance].values() for at in times]
        in_service = seconds_covered(taken, window.started, window.drained)
        identities |= set(served)

        said = _said(launches.get(instance, {}), window)
        valid = {one for (one,) in said if _FINGERPRINT.fullmatch(one)}
        whole = len(said) == 1 and len(valid) == 1 and max(said.values()) >= in_service - ALLOWANCE_SECONDS
        launched.append((valid, whole))

        def of(key: Key, instance: str = instance) -> list[Series]:
            return by_key.get(key, {}).get(instance, [])

        counters = {metric: _counted(of(metric), window) for metric in COUNTERS}
        output = counters[HostMetric.OUTPUT_TOKENS_TOTAL].increase
        warm_up = driver.warm_up
        worked = (
            None
            if warm_up is None
            else _counted(_until(of(HostMetric.OUTPUT_TOKENS_TOTAL), warm_up.drained), warm_up).increase
        )
        liveness = 0 if warm_up is None else int(warm_up.seconds // PROBE_INTERVAL_SECONDS) + 1
        usage = _scrapes(of(Source.CACHE_USAGE), max)
        capacity = _scrapes(of(Source.CACHE_CAPACITY), max)
        kinds = {kind for kind, _, _ in served}
        hosts.append(
            Host(
                host=ordinals.of(instance),
                instance_type=InstanceType(next(iter(kinds)))
                if len(kinds) == 1 and kinds <= set(InstanceType)
                else None,
                in_service_seconds=in_service,
                whole_window=in_service >= window.seconds - ALLOWANCE_SECONDS,
                counters=counters,
                gauges={metric: _level(of(metric), window, combine) for metric, combine in GAUGES.items()},
                output_tokens_per_s=None if output is None else output / window.seconds,
                warm_up_output_tokens=worked,
                warmed=worked is not None and worked > liveness,
                cache=Cache(
                    capacity_tokens=_last(capacity, window.started - ALLOWANCE_SECONDS, window.drained),
                    usage_at_start=_last(usage, window.started - ALLOWANCE_SECONDS, window.started),
                    usage_peak=max(
                        (value for at, value in usage if window.started <= at <= window.drained), default=None
                    ),
                ),
            )
        )

    turnover = any(not host.whole_window for host in hosts)
    types = {host.instance_type for host in hosts}
    instance_type = next(iter(types)) if len(types) == 1 else None
    gpu_type, gpus = INSTANCE_GPUS[instance_type] if instance_type is not None else (None, None)
    image = _one({image for _, image, _ in identities}, _DIGEST)
    revision = _one({revision for _, _, revision in identities}, _COMMIT)
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
    }
    seen = Settings(**observed)
    asked = requested.settings

    if asked.host_count is None and asked.instance_type is None:
        membership = Reason.NOT_DECLARED
    elif not hosts:
        membership = Reason.NOT_OBSERVED
    elif turnover:
        membership = Reason.HOST_TURNOVER
    elif len(types) > 1:
        membership = Reason.MIXED
    else:
        membership = _match((asked.host_count, asked.instance_type), (seen.host_count, seen.instance_type))
    release = _match((asked.engine_image_digest, asked.model_revision), (image, revision))
    if release is not Reason.NOT_DECLARED and len({served[1:] for served in identities}) > 1:
        release = Reason.MIXED
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
        treatments=treatments,
        reasons=_reasons(hosts, traffic),
        hosts=tuple(sorted(hosts, key=lambda host: host.host)),
        pool=Totals(totals, None if output is None else output / window.seconds),
        traffic=traffic,
        preflight=driver.preflight,
    )
