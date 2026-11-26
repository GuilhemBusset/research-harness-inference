"""The telemetry reader: what a window's series establish, and what they leave unverified.

Every series is made up: a workspace of two hosts scraped every 15 seconds, a gateway task and a
canary cohort of two, over a window of 300 seconds. Each test starts from that clean workspace,
which reads as verified with no flag, and changes one thing.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import common
from evaluate import telemetry
from evaluate.contracts import GpuType, HostMetric, InstanceType, InvalidFlag, Quantization, Settings
from evaluate.telemetry import (
    Cache,
    Coverage,
    Driver,
    Ordinals,
    Preflight,
    Reason,
    Requested,
    Source,
    Treatment,
    Window,
)
from rhi_common.engine_metrics import VLLM, Further, Quantity, further_names, metric_names

if TYPE_CHECKING:
    from collections.abc import Callable

POOL = "coursework"
START, END = 1_000.0, 1_300.0
WINDOW = Window(START, END)
WARM_UP = Window(820.0, 940.0)
# The scrapes: one 12 seconds before the window, one 3 seconds after its drain.
SCRAPES = tuple(703.0 + 15 * n for n in range(46))
# The node's own completions during the window, one a minute, of one token each.
LIVENESS = (1_030.0, 1_090.0, 1_150.0, 1_210.0, 1_270.0)

HOST_A, HOST_B, HOST_C = "node-a-private", "node-b-private", "node-c-private"
TASK = "gateway-task-private"
COHORT = ("cohort-one", "cohort-two")

IMAGE, OTHER_IMAGE = "sha256:" + "a1" * 32, "sha256:" + "b2" * 32
REVISION, TOKENIZER = "c3" * 20, "d4" * 20
PROFILE = "sha256:" + "e5" * 32
FINGERPRINT, OTHER_FINGERPRINT = "f6" * 32, "07" * 32

NAMES, FURTHER = metric_names(VLLM), further_names(VLLM)
OUTPUT = f"{NAMES[Quantity.OUTPUT_TOKENS]}_total"
INPUT = f"{NAMES[Quantity.INPUT_TOKENS]}_total"
CACHED = f"{NAMES[Quantity.CACHED_INPUT_TOKENS]}_total"
BY_SOURCE = f"{FURTHER[Further.COMPUTED_INPUT_TOKENS]}_total"
PREEMPTIONS = f"{FURTHER[Further.PREEMPTIONS]}_total"
RUNNING, WAITING = str(NAMES[Quantity.RUNNING_REQUESTS]), str(NAMES[Quantity.WAITING_REQUESTS])
CACHE_USAGE = str(NAMES[Quantity.CACHE_USAGE])
CACHE_CAPACITY = "rhi_lifecycle_engine_cache_capacity_tokens"
RELEASE, LAUNCH = "rhi_lifecycle_release_info", "rhi_lifecycle_launch_info"
GPU = "DCGM_FI_DEV_GPU_UTIL_RATIO"
GATEWAY = "rhi_gateway_student_requests_total"

_QUERY = re.compile(r"(?P<name>[^{]+)\{(?P<matchers>[^}]*)\}\[(?P<span>\d+)(?P<unit>ms|s)\]")
_MATCHER = re.compile(r'(\w+)="([^"]*)"')


@dataclass
class Held:
    name: str
    labels: dict[str, str]
    value: Callable[[float], float]
    times: tuple[float, ...]


class Workspace:
    """A workspace that answers a range selector with the samples it holds, as scraped."""

    def __init__(self) -> None:
        self.held: list[Held] = []

    def add(
        self, name: str, value: Callable[[float], float], times: tuple[float, ...] = SCRAPES, **labels: str
    ) -> None:
        self.held.append(Held(name, {"pool": POOL, **labels}, value, times))

    def find(self, name: str | None = None, **labels: str) -> list[Held]:
        return [one for one in self.held if name in (None, one.name) and labels.items() <= one.labels.items()]

    def drop(self, name: str | None = None, **labels: str) -> None:
        gone = self.find(name, **labels)
        assert gone
        self.held = [one for one in self.held if all(one is not other for other in gone)]

    def read_samples(self, query: str, *, at: float) -> list[common.Series]:
        asked = _QUERY.fullmatch(query)
        assert asked is not None, query
        wanted = dict(_MATCHER.findall(asked["matchers"]))
        seconds = int(asked["span"]) / (1000 if asked["unit"] == "ms" else 1)
        found = []
        for one in self.held:
            if one.name == asked["name"] and wanted.items() <= one.labels.items():
                samples = tuple((t, float(one.value(t))) for t in one.times if at - seconds < t <= at)
                if samples:
                    found.append(common.Series({"__name__": one.name, **one.labels}, samples))
        return found

    def read_series(self, query: str, *, start: float, end: float, step: float) -> list[common.Series]:
        raise AssertionError("the reader takes samples as they were scraped")


def within(at: float, low: float, high: float) -> float:
    """The seconds of `low` to `high` that have passed at `at`."""
    return min(max(at - low, 0.0), high - low)


def tokens(
    per_second: float, *, warm: float = 5.0, liveness: tuple[float, ...] = (), base: float = 50.0
) -> Callable[[float], float]:
    """A counter that adds `warm` a second through the warm-up and `per_second` through the
    window, to its drain and no further, and one token at each time of `liveness`."""

    def value(at: float) -> float:
        counted = warm * within(at, WARM_UP.started, WARM_UP.drained) + per_second * within(at, START, END)
        return base + counted + sum(1 for sent in liveness if sent <= at)

    return value


def requests(count: int, *, base: int = 0) -> Callable[[float], float]:
    """A counter of `count` requests spread over the window, after `base` requests before it."""
    return lambda at: base + int(count * within(at, START, END) / (END - START))


def add_host(
    workspace: Workspace,
    host: str,
    rate: float,
    *,
    liveness: tuple[float, ...] = (),
    times: tuple[float, ...] = SCRAPES,
) -> None:
    def add(name: str, value: Callable[[float], float], **labels: str) -> None:
        workspace.add(name, value, times, instance=host, instance_type="g7e.2xlarge", **labels)

    add(RELEASE, lambda _: 1, image_digest=IMAGE, model_revision=REVISION)
    add(LAUNCH, lambda _: 1, settings_fingerprint=FINGERPRINT)
    add(OUTPUT, tokens(rate, liveness=liveness), engine="0")
    add(INPUT, tokens(10 * rate), engine="0")
    add(CACHED, tokens(5 * rate), engine="0")
    add(BY_SOURCE, tokens(4 * rate), engine="0", source="local_compute")
    add(BY_SOURCE, tokens(5 * rate), engine="0", source="local_cache_hit")
    add(PREEMPTIONS, lambda at: (at >= 1_100) + (at >= 1_200) if host == HOST_B else 0, engine="0")
    add(RUNNING, lambda _: 4, engine="0")
    add(WAITING, lambda at: 3 if at >= 1_150 else 0, engine="0")
    add(CACHE_USAGE, lambda at: 0.5 if at >= 1_100 else 0.25, engine="0")
    add(CACHE_CAPACITY, lambda _: 400_000)
    add(GPU, lambda _: 0.9, gpu="0")


def clean(*, liveness: tuple[float, ...] = ()) -> Workspace:
    """Two hosts through the whole window, at 10 and 20 output tokens a second, and the cohort's
    40 requests at the gateway, after 3 setup probes sent well before the window."""
    workspace = Workspace()
    add_host(workspace, HOST_A, 10, liveness=liveness)
    add_host(workspace, HOST_B, 20, liveness=liveness)
    workspace.add(GATEWAY, requests(24, base=3), alias=COHORT[0], instance=TASK)
    workspace.add(GATEWAY, requests(16), alias=COHORT[1], instance=TASK)
    return workspace


def settings(**given: Any) -> Settings:
    return Settings(**{**dict.fromkeys(spec.name for spec in dataclasses.fields(Settings)), **given})


REQUESTED = Requested(
    settings(
        host_count=2,
        instance_type=InstanceType.G7E_2XLARGE,
        model_revision=REVISION,
        tokenizer_revision=TOKENIZER,
        quantization=Quantization.BITS_4,
        engine_image_digest=IMAGE,
        gateway_profile_digest=PROFILE,
    ),
    launch_fingerprint=FINGERPRINT,
)
# 36 attempts and 4 side queries reached the gateway: the cohort's 40 requests.
DRIVER = Driver(
    cohort=COHORT,
    attempts=36,
    side_queries=4,
    warm_up=WARM_UP,
    tokenizer_revision=TOKENIZER,
    gateway_profile_digest=PROFILE,
)


def reading(
    workspace: Workspace, *, window: Window = WINDOW, driver: Driver = DRIVER, ordinals: Ordinals | None = None
) -> telemetry.Reading:
    answers = telemetry.read(workspace, POOL, VLLM, window, driver.warm_up)
    return telemetry.resolve(answers, window, REQUESTED, driver, ordinals)


def increases(read: telemetry.Reading, metric: HostMetric) -> list[float | None]:
    return [host.counters[metric].increase for host in read.hosts]


# --- Requested and observed --------------------------------------------------------------------


def test_a_clean_window_is_verified_and_keeps_requested_and_observed_apart() -> None:
    read = reading(clean())

    assert read.unverified == ()
    assert (read.reasons, read.flags) == ((), ())
    assert read.requested is REQUESTED.settings
    observed = read.observed
    assert (observed.host_count, observed.instance_type) == (2, InstanceType.G7E_2XLARGE)
    assert (observed.gpu_type, observed.gpus_per_host) == (GpuType.RTX_PRO_6000, 1)
    assert (observed.engine_image_digest, observed.model_revision) == (IMAGE, REVISION)
    assert (observed.tokenizer_revision, observed.gateway_profile_digest) == (TOKENIZER, PROFILE)
    # No series says the quantization, and the request does not stand in for one.
    assert read.requested.quantization is Quantization.BITS_4
    assert observed.quantization is None


def test_a_release_other_than_the_requested_one_is_reported_and_not_raised() -> None:
    workspace = clean()
    for release in workspace.find(RELEASE):
        release.labels["image_digest"] = OTHER_IMAGE
    read = reading(workspace)
    assert read.treatments[Treatment.RELEASE] is Reason.UNEXPECTED
    assert read.observed.engine_image_digest == OTHER_IMAGE
    assert read.requested.engine_image_digest == IMAGE

    workspace.find(RELEASE, instance=HOST_A)[0].labels["image_digest"] = IMAGE
    read = reading(workspace)
    assert read.treatments[Treatment.RELEASE] is Reason.MIXED
    assert read.observed.engine_image_digest is None
    assert read.unverified == (Treatment.RELEASE,)


def test_two_fingerprints_at_one_revision_leave_the_launch_settings_unverified() -> None:
    workspace = clean()
    workspace.find(LAUNCH, instance=HOST_B)[0].labels["settings_fingerprint"] = OTHER_FINGERPRINT
    read = reading(workspace)

    assert read.unverified == (Treatment.LAUNCH_SETTINGS,)
    assert read.treatments[Treatment.LAUNCH_SETTINGS] is Reason.MIXED
    # The image and the model revision are the same on both hosts, and still the requested ones.
    assert read.treatments[Treatment.RELEASE] is None
    assert (read.observed.engine_image_digest, read.observed.model_revision) == (IMAGE, REVISION)
    assert read.flags == ()

    workspace.find(LAUNCH, instance=HOST_B)[0].labels["settings_fingerprint"] = "unavailable"
    assert reading(workspace).treatments[Treatment.LAUNCH_SETTINGS] is Reason.NOT_OBSERVED


def test_no_gateway_provenance_leaves_only_the_request_side_unverified() -> None:
    read = reading(clean(), driver=replace(DRIVER, gateway_profile_digest=None))

    assert read.unverified == (Treatment.GATEWAY_PROFILE,)
    assert read.treatments[Treatment.GATEWAY_PROFILE] is Reason.NOT_OBSERVED
    assert read.observed.gateway_profile_digest is None
    assert read.treatments[Treatment.RELEASE] is None
    assert read.treatments[Treatment.LAUNCH_SETTINGS] is None
    assert read.flags == ()


# --- The hosts' metrics ------------------------------------------------------------------------


def test_each_host_reports_its_counters_and_gauges_through_the_drain() -> None:
    read = reading(clean())
    first, second = read.hosts

    # The last 12 seconds of output reach the workspace with the scrape after the drain.
    assert increases(read, HostMetric.OUTPUT_TOKENS_TOTAL) == [3_000, 6_000]
    assert increases(read, HostMetric.INPUT_TOKENS_TOTAL) == [30_000, 60_000]
    assert increases(read, HostMetric.CACHED_INPUT_TOKENS_TOTAL) == [15_000, 30_000]
    # The engine's own count of what it computed, not the input less the cached input.
    assert increases(read, HostMetric.COMPUTED_INPUT_TOKENS_TOTAL) == [12_000, 24_000]
    assert increases(read, HostMetric.PREEMPTIONS_TOTAL) == [0, 2]
    assert {counted.coverage for host in read.hosts for counted in host.counters.values()} == {Coverage.COMPLETE}
    running, waiting = first.gauges[HostMetric.RUNNING_REQUESTS], first.gauges[HostMetric.WAITING_REQUESTS]
    assert (running.mean, running.peak) == (4, 4)
    # Nothing waits for the first half of the window's scrapes, three requests for the second.
    assert (waiting.mean, waiting.peak, waiting.coverage) == (1.5, 3, Coverage.COMPLETE)
    assert second.gauges[HostMetric.GPU_UTILIZATION].mean == 0.9
    assert read.pool.counters[HostMetric.COMPUTED_INPUT_TOKENS_TOTAL] == 36_000


def test_a_counter_that_restarted_is_counted_across_the_restart() -> None:
    workspace = clean()
    # The engine of the first host restarts just after the scrape at 1138 and counts from zero.
    before = tokens(10)
    workspace.find(OUTPUT, instance=HOST_A)[0].value = lambda at: (
        before(at) if at <= 1_138 else 10 * within(at, 1_138, END)
    )
    read = reading(workspace)

    output = read.hosts[0].counters[HostMetric.OUTPUT_TOKENS_TOTAL]
    assert (output.increase, output.resets, output.coverage) == (3_000, 1, Coverage.COMPLETE)
    assert read.hosts[1].counters[HostMetric.OUTPUT_TOKENS_TOTAL].resets == 0
    assert read.pool.counters[HostMetric.OUTPUT_TOKENS_TOTAL] == 9_000
    assert read.unverified == ()
    assert (read.reasons, read.flags) == ((), ())


def test_a_host_with_no_preemption_series_reads_missing_and_the_cell_lacks_evidence() -> None:
    workspace = clean()
    workspace.drop(PREEMPTIONS, instance=HOST_B)
    read = reading(workspace)

    preemptions = read.hosts[1].counters[HostMetric.PREEMPTIONS_TOTAL]
    assert (preemptions.increase, preemptions.coverage) == (None, Coverage.MISSING)
    assert read.hosts[0].counters[HostMetric.PREEMPTIONS_TOTAL].increase == 0
    assert read.reasons == (Reason.PREEMPTION_COVERAGE_MISSING,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
    # What the pool served is still established, and the other observations are kept.
    assert read.unverified == ()
    assert increases(read, HostMetric.OUTPUT_TOKENS_TOTAL) == [3_000, 6_000]


def test_a_hole_in_a_series_is_incomplete_coverage_and_keeps_what_was_read() -> None:
    workspace = clean()
    workspace.find(OUTPUT, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if not 1_100 < at < 1_200)
    read = reading(workspace)

    output = read.hosts[0].counters[HostMetric.OUTPUT_TOKENS_TOTAL]
    assert (output.increase, output.coverage) == (3_000, Coverage.INCOMPLETE)
    assert read.reasons == (Reason.INCOMPLETE_COVERAGE,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)


def test_rates_and_totals_are_over_the_seconds_that_elapsed_through_the_drain() -> None:
    on_time = reading(clean())
    # The same work, with a drain that lasted a minute longer.
    late = reading(clean(), window=Window(START, END + 60))

    assert (on_time.seconds, late.seconds) == (300, 360)
    assert [host.output_tokens_per_s for host in on_time.hosts] == [10, 20]
    assert on_time.pool.output_tokens_per_s == 30
    assert [host.output_tokens_per_s for host in late.hosts] == [3_000 / 360, 6_000 / 360]
    assert late.pool.output_tokens_per_s == 9_000 / 360
    assert late.pool.counters == on_time.pool.counters
    assert late.pool.counters[HostMetric.OUTPUT_TOKENS_TOTAL] == 9_000
    assert late.flags == ()


# --- Membership --------------------------------------------------------------------------------


def test_a_host_replaced_during_the_window_is_a_turnover_under_local_ordinals() -> None:
    ordinals = Ordinals()
    assert [host.host for host in reading(clean(), ordinals=ordinals).hosts] == [0, 1]

    workspace = clean()
    for one in workspace.find(instance=HOST_B):
        one.times = tuple(at for at in SCRAPES if at <= 1_150)
    add_host(workspace, HOST_C, 20, times=tuple(at for at in SCRAPES if at >= 1_165))
    read = reading(workspace, ordinals=ordinals)

    assert read.reasons == (Reason.HOST_TURNOVER,)
    assert read.flags == (InvalidFlag.HOST_TURNOVER,)
    assert read.unverified == (Treatment.HOSTS,)
    assert read.treatments[Treatment.HOSTS] is Reason.HOST_TURNOVER
    # The first host keeps its ordinal, and the newcomer takes the next one.
    assert [(host.host, host.whole_window) for host in read.hosts] == [(0, True), (1, False), (2, False)]
    assert read.observed.host_count == 3
    assert read.hosts[2].counters[HostMetric.OUTPUT_TOKENS_TOTAL].increase is not None


def test_a_scrape_at_each_edge_of_the_window_is_allowed_before_a_host_counts_as_late() -> None:
    workspace = clean()
    release = workspace.find(RELEASE, instance=HOST_A)[0]
    release.times = tuple(at for at in SCRAPES if START < at < END)
    assert reading(workspace).flags == ()

    release.times = tuple(at for at in SCRAPES if START + 45 < at < END)
    assert reading(workspace).flags == (InvalidFlag.HOST_TURNOVER,)


def test_no_private_label_reaches_a_reading() -> None:
    workspace = clean()
    workspace.add(GATEWAY, requests(3), alias="someone-else", instance=TASK)
    written = repr(reading(workspace))

    for private in (HOST_A, HOST_B, *COHORT, "someone-else", TASK):
        assert private not in written


# --- Traffic at the gateway --------------------------------------------------------------------


def test_smoke_traffic_through_the_gateway_is_external_and_the_nodes_liveness_is_not() -> None:
    quiet = reading(clean(liveness=LIVENESS))
    # The liveness completions never pass the gateway: they are engine output, and flag nothing.
    assert increases(quiet, HostMetric.OUTPUT_TOKENS_TOTAL) == [3_005, 6_005]
    assert quiet.pool.counters[HostMetric.OUTPUT_TOKENS_TOTAL] == 9_010
    assert quiet.pool.output_tokens_per_s == 9_010 / 300
    assert (quiet.traffic.external_requests, quiet.flags) == (0, ())

    workspace = clean(liveness=LIVENESS)
    workspace.add(GATEWAY, requests(3, base=7), alias="deploy-smoke", instance=TASK)
    smoked = reading(workspace)
    assert (smoked.traffic.pool_requests, smoked.traffic.cohort_requests) == (43, 40)
    assert smoked.traffic.external_requests == 3
    assert smoked.reasons == (Reason.EXTERNAL_TRAFFIC,)
    assert smoked.flags == (InvalidFlag.EXTERNAL_TRAFFIC,)
    assert smoked.pool.counters == quiet.pool.counters
    assert smoked.unverified == ()


def test_setup_probes_are_in_neither_the_cohorts_count_nor_the_external_one() -> None:
    # The clean cohort sent 3 probes well before the window: the scrape before it holds them.
    settled = reading(clean()).traffic
    assert (settled.pool_requests, settled.cohort_requests, settled.setup_probes_excluded) == (40, 40, 0)

    # A probe sent at 995, after that scrape, is in the first scrape of the window.
    workspace = clean()
    workspace.find(GATEWAY, alias=COHORT[0])[0].value = lambda at: requests(24, base=3)(at) + (at >= 995)
    read = reading(workspace, driver=replace(DRIVER, setup_probes=1))
    assert (read.traffic.cohort_requests, read.traffic.setup_probes_excluded) == (40, 1)
    assert (read.traffic.external_requests, read.traffic.reconciled, read.flags) == (0, True, ())

    # The driver names it, or the counters do not reconcile.
    assert reading(workspace).flags == (InvalidFlag.UNRECONCILED_COUNTERS,)


def test_the_cohorts_count_reconciles_with_attempts_and_side_queries_together() -> None:
    assert reading(clean()).traffic.driver_requests == 40

    forgotten = reading(clean(), driver=replace(DRIVER, side_queries=0))
    assert not forgotten.traffic.reconciled
    assert forgotten.reasons == (Reason.UNRECONCILED_COUNTERS,)
    assert forgotten.traffic.external_requests == 0

    workspace = clean()
    workspace.drop(GATEWAY)
    silent = reading(workspace)
    assert silent.traffic.pool_requests is None
    assert silent.reasons == (Reason.GATEWAY_COUNTERS_MISSING,)


# --- Preflight, warm-up and cache --------------------------------------------------------------


def test_a_reading_records_the_preflight_the_warm_up_of_every_host_and_its_cache() -> None:
    workspace = clean()
    check = telemetry.preflight(workspace, POOL, VLLM, at=START)
    assert check == Preflight(hosts=2, missing=())

    read = reading(workspace, driver=replace(DRIVER, preflight=check))
    assert read.preflight is check
    # 5 tokens a second, seen through the last scrape of the warm-up.
    assert [(host.warm_up_output_tokens, host.warmed) for host in read.hosts] == [(540, True), (540, True)]
    assert read.hosts[0].cache == Cache(capacity_tokens=400_000, usage_at_start=0.25, usage_peak=0.5)
    assert read.hosts[0].counters[HostMetric.CACHED_INPUT_TOKENS_TOTAL].increase == 15_000


def test_a_host_that_only_answered_its_own_liveness_was_not_warmed_up() -> None:
    workspace = clean()
    workspace.find(OUTPUT, instance=HOST_B)[0].value = tokens(20, warm=0, liveness=(850.0, 910.0))
    read = reading(workspace)

    assert [(host.warm_up_output_tokens, host.warmed) for host in read.hosts] == [(540, True), (2, False)]
    assert read.reasons == (Reason.WARM_UP_INCOMPLETE,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
    assert reading(clean(), driver=replace(DRIVER, warm_up=None)).reasons == (Reason.WARM_UP_INCOMPLETE,)


def test_missing_cache_and_preemption_evidence_shows_in_the_preflight_and_the_reading() -> None:
    workspace = clean()
    workspace.drop(CACHE_CAPACITY, instance=HOST_A)
    workspace.drop(PREEMPTIONS, instance=HOST_B)

    assert telemetry.preflight(workspace, POOL, VLLM, at=START).missing == (
        Source.CACHE_CAPACITY,
        HostMetric.PREEMPTIONS_TOTAL,
    )
    read = reading(workspace)
    assert read.hosts[0].cache.capacity_tokens is None
    assert set(read.reasons) == {Reason.PREEMPTION_COVERAGE_MISSING, Reason.CACHE_EVIDENCE_MISSING}
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
