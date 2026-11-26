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
from evaluate.contracts import GpuType, HostMetric, InstanceType, InvalidFlag, KvDtype, Quantization, Settings
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
from rhi_common.engine_metrics import VLLM, Further, Quantity, cache_capacity, further_names, metric_names

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
CACHE_CONFIG = "vllm:cache_config_info"
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
    workspace: Workspace,
    *,
    window: Window = WINDOW,
    requested: Requested = REQUESTED,
    driver: Driver = DRIVER,
    ordinals: Ordinals | None = None,
) -> telemetry.Reading:
    answers = telemetry.read(workspace, POOL, VLLM, window, driver.warm_up)
    return telemetry.resolve(answers, window, requested, driver, ordinals)


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
    assert (read.requested_launch_fingerprint, read.observed_launch_fingerprint) == (FINGERPRINT, FINGERPRINT)
    assert [(host.image_digest, host.model_revision) for host in read.hosts] == [(IMAGE, REVISION)] * 2


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

    # Each host's fingerprint is kept, so the two configurations that served can be told apart.
    assert [host.launch_fingerprints for host in read.hosts] == [(FINGERPRINT,), (OTHER_FINGERPRINT,)]
    assert read.observed_launch_fingerprint is None

    workspace.find(LAUNCH, instance=HOST_B)[0].labels["settings_fingerprint"] = "unavailable"
    assert reading(workspace).treatments[Treatment.LAUNCH_SETTINGS] is Reason.NOT_OBSERVED


def test_a_fingerprint_that_is_not_the_requested_one_is_kept_as_observed() -> None:
    def served(fingerprint: str) -> telemetry.Reading:
        workspace = clean()
        for launch in workspace.find(LAUNCH):
            launch.labels["settings_fingerprint"] = fingerprint
        return reading(workspace)

    one, other = served(OTHER_FINGERPRINT), served("12" * 32)
    assert one.treatments[Treatment.LAUNCH_SETTINGS] is Reason.UNEXPECTED
    assert other.treatments[Treatment.LAUNCH_SETTINGS] is Reason.UNEXPECTED
    assert (one.observed_launch_fingerprint, other.observed_launch_fingerprint) == (OTHER_FINGERPRINT, "12" * 32)
    assert one.requested_launch_fingerprint == FINGERPRINT


def test_the_launch_settings_need_the_fingerprint_through_the_window() -> None:
    # Three scrapes of the launch metric never arrived: a minute nothing says the fingerprint of.
    workspace = clean()
    workspace.find(LAUNCH, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if not 1_108 < at < 1_168)
    read = reading(workspace)
    assert read.unverified == (Treatment.LAUNCH_SETTINGS,)
    assert read.treatments[Treatment.LAUNCH_SETTINGS] is Reason.INCOMPLETE_COVERAGE
    assert read.hosts[0].launch_fingerprints == (FINGERPRINT,)

    # Samples that are no number say nothing.
    workspace = clean()
    workspace.find(LAUNCH, instance=HOST_A)[0].value = lambda _: float("nan")
    read = reading(workspace)
    assert read.treatments[Treatment.LAUNCH_SETTINGS] is Reason.NOT_OBSERVED
    assert read.hosts[0].launch_fingerprints == ()


def test_an_agent_that_names_no_engine_beyond_the_allowance_leaves_the_window_without_evidence() -> None:
    def unnamed(scrapes: int) -> telemetry.Reading:
        # The first host's agent says no fingerprint at `scrapes` scrapes from 1108 on, and no
        # fingerprint is requested.
        workspace = clean()
        gap = SCRAPES[27 : 27 + scrapes]
        workspace.find(LAUNCH, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if at not in gap)
        workspace.add(LAUNCH, lambda _: 1, gap, instance=HOST_A, settings_fingerprint="unavailable")
        return reading(workspace, requested=replace(REQUESTED, launch_fingerprint=None))

    # Two scrapes: 45 seconds, from the fingerprint at 1093 to the one at 1138, name no engine.
    read = unnamed(2)
    assert read.treatments[Treatment.LAUNCH_SETTINGS] is Reason.NOT_DECLARED
    assert [host.launch_unnamed for host in read.hosts] == [True, False]
    assert read.reasons == (Reason.INCOMPLETE_COVERAGE,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
    # One scrape is within the allowance, as a scrape that never arrived would be.
    assert unnamed(1).reasons == ()


def test_the_effective_settings_an_engine_exposes_are_observed_where_every_host_agrees() -> None:
    place = cache_capacity(VLLM)
    assert place is not None
    assert place.metric == CACHE_CONFIG
    # No engine exposes them in the clean workspace: the request says the cache's data type, and
    # the observed settings do not repeat it.
    assert reading(clean()).observed.prefix_caching is None

    workspace = clean()
    for host in (HOST_A, HOST_B):
        workspace.add(
            CACHE_CONFIG, lambda _: 1, instance=host, engine="0", enable_prefix_caching="True", cache_dtype="fp8"
        )
    read = reading(workspace)
    assert (read.observed.prefix_caching, read.observed.kv_dtype) == (True, KvDtype.BITS_8)
    assert read.requested.prefix_caching is None

    workspace.find(CACHE_CONFIG, instance=HOST_B)[0].labels["enable_prefix_caching"] = "False"
    read = reading(workspace)
    assert (read.observed.prefix_caching, read.observed.kv_dtype) == (None, KvDtype.BITS_8)

    # A host that said it for the start of the window only did not say it through the window.
    workspace.find(CACHE_CONFIG, instance=HOST_B)[0].times = tuple(at for at in SCRAPES if at < 1_100)
    assert reading(workspace).observed.kv_dtype is None


def test_a_fingerprint_first_said_after_the_scrape_that_closed_the_window_is_not_the_windows() -> None:
    workspace = clean()
    # The agent is scraped on the drain, and says another fingerprint from 14 seconds later on.
    launch = workspace.find(LAUNCH, instance=HOST_A)[0]
    launch.times = (*(at for at in SCRAPES if at < END), END)
    workspace.add(LAUNCH, lambda _: 1, (END + 14, END + 29), instance=HOST_A, settings_fingerprint=OTHER_FINGERPRINT)
    read = reading(workspace)
    assert (read.unverified, read.reasons) == ((), ())
    assert read.hosts[0].launch_fingerprints == (FINGERPRINT,)

    # With no scrape on the drain the next one closes the window, and what it is the first to say
    # may have begun inside.
    launch.times = tuple(at for at in SCRAPES if at < END)
    workspace.find(LAUNCH, settings_fingerprint=OTHER_FINGERPRINT)[0].times = (END + 3, END + 18)
    assert reading(workspace).treatments[Treatment.LAUNCH_SETTINGS] is Reason.MIXED


def test_an_engine_that_says_another_setting_contradicts_a_matching_fingerprint() -> None:
    asked = Requested(replace(REQUESTED.settings, prefix_caching=True), FINGERPRINT)
    workspace = clean()
    for host, enabled in ((HOST_A, "True"), (HOST_B, "False")):
        workspace.add(CACHE_CONFIG, lambda _: 1, instance=host, engine="0", enable_prefix_caching=enabled)
    read = reading(workspace, requested=asked)

    assert read.unverified == (Treatment.LAUNCH_SETTINGS,)
    assert read.treatments[Treatment.LAUNCH_SETTINGS] is Reason.CONTRADICTED
    # The fingerprint is the requested one on every host, and is kept as what was observed.
    assert (read.requested_launch_fingerprint, read.observed_launch_fingerprint) == (FINGERPRINT, FINGERPRINT)
    assert read.flags == ()

    # Every engine says what was requested: the fingerprint verifies the launch settings.
    workspace.find(CACHE_CONFIG, instance=HOST_B)[0].labels["enable_prefix_caching"] = "True"
    read = reading(workspace, requested=asked)
    assert (read.unverified, read.observed.prefix_caching) == ((), True)

    # An engine that said it for a part of the window only still said it.
    workspace.add(
        CACHE_CONFIG,
        lambda _: 1,
        tuple(at for at in SCRAPES if 1_100 < at < 1_200),
        instance=HOST_A,
        engine="0",
        enable_prefix_caching="False",
    )
    assert reading(workspace, requested=asked).treatments[Treatment.LAUNCH_SETTINGS] is Reason.CONTRADICTED


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


def test_a_counter_far_into_its_engines_life_still_measures_the_windows_increase() -> None:
    workspace = clean()
    # The engine has counted 11 billion input tokens before the window: more than any count of a
    # window may be, and no reason to doubt the 30 000 it adds.
    workspace.find(INPUT, instance=HOST_A)[0].value = tokens(100, base=11_000_000_000)
    read = reading(workspace)
    measured = read.hosts[0].counters[HostMetric.INPUT_TOKENS_TOTAL]
    assert (measured.increase, measured.coverage) == (30_000, Coverage.COMPLETE)
    assert read.flags == ()
    assert telemetry.preflight(workspace, POOL, VLLM, at=START).missing == ()

    # An increase beyond that bound is no count of a window: not kept, and not complete evidence.
    workspace.find(INPUT, instance=HOST_A)[0].value = tokens(100_000_000)
    read = reading(workspace)
    measured = read.hosts[0].counters[HostMetric.INPUT_TOKENS_TOTAL]
    assert (measured.increase, measured.coverage) == (None, Coverage.INCOMPLETE)
    assert read.pool.counters[HostMetric.INPUT_TOKENS_TOTAL] == 60_000
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)


def test_a_host_with_no_preemption_series_reads_missing_and_the_cell_lacks_evidence() -> None:
    workspace = clean()
    workspace.drop(PREEMPTIONS, instance=HOST_B)
    read = reading(workspace)

    preemptions = read.hosts[1].counters[HostMetric.PREEMPTIONS_TOTAL]
    assert (preemptions.increase, preemptions.coverage) == (None, Coverage.MISSING)
    assert read.hosts[0].counters[HostMetric.PREEMPTIONS_TOTAL].increase == 0
    # The series is missing from the warm-up as well, so the host's warm-up is not covered.
    assert read.reasons == (Reason.PREEMPTION_COVERAGE_MISSING, Reason.WARM_UP_INCOMPLETE)
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


def test_a_counter_with_no_sample_before_the_window_counts_only_the_rises_it_shows() -> None:
    workspace = clean()
    workspace.find(OUTPUT, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if at > START)
    output = reading(workspace).hosts[0].counters[HostMetric.OUTPUT_TOKENS_TOTAL]

    # From the scrape at 1003 on: the 650 tokens that one held are not the window's work.
    assert (output.increase, output.coverage) == (2_970, Coverage.INCOMPLETE)


def test_what_an_engine_counts_after_the_drain_is_not_the_windows() -> None:
    def late(workspace: Workspace) -> None:
        # 100 more tokens, ten seconds after the drain.
        output = workspace.find(OUTPUT, instance=HOST_A)[0]
        before = output.value
        output.value = lambda at: before(at) + 100 * (at >= END + 10)

    # A scrape fell on the drain: it closes the window, and the next ones add nothing.
    workspace = clean()
    output = workspace.find(OUTPUT, instance=HOST_A)[0]
    output.times = tuple(sorted((*output.times, END)))
    late(workspace)
    read = reading(workspace)
    assert read.pool.counters[HostMetric.OUTPUT_TOKENS_TOTAL] == 9_000
    assert read.pool.output_tokens_per_s == 30

    # No scrape on the drain: the one at 1303 closes it, and the one at 1318 is not read into it.
    workspace = clean()
    late(workspace)
    assert reading(workspace).pool.counters[HostMetric.OUTPUT_TOKENS_TOTAL] == 9_000


def test_a_gauge_is_incomplete_when_one_of_its_series_stops() -> None:
    workspace = clean()
    # A second engine index reports 4 running requests, but not from 1050 to 1200.
    workspace.add(
        RUNNING, lambda _: 4, tuple(at for at in SCRAPES if not 1_050 < at < 1_200), instance=HOST_A, engine="1"
    )
    read = reading(workspace)

    running = read.hosts[0].gauges[HostMetric.RUNNING_REQUESTS]
    # The host's level is read where both engines reported, never from one of them alone.
    assert (running.mean, running.peak, running.coverage) == (8, 8, Coverage.INCOMPLETE)
    assert read.hosts[1].gauges[HostMetric.RUNNING_REQUESTS].coverage is Coverage.COMPLETE
    assert read.reasons == (Reason.INCOMPLETE_COVERAGE,)


def test_a_gauge_whose_series_share_no_scrape_has_no_level_and_does_not_cover_the_window() -> None:
    workspace = clean()
    # Two engine indexes report on alternate scrapes: each has a sample every 30 seconds.
    workspace.find(RUNNING, instance=HOST_A)[0].times = SCRAPES[::2]
    workspace.add(RUNNING, lambda _: 1, SCRAPES[1::2], instance=HOST_A, engine="1")
    read = reading(workspace)

    running = read.hosts[0].gauges[HostMetric.RUNNING_REQUESTS]
    assert (running.mean, running.peak, running.coverage) == (None, None, Coverage.INCOMPLETE)
    assert Reason.INCOMPLETE_COVERAGE in read.reasons
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
    assert read.hosts[1].gauges[HostMetric.RUNNING_REQUESTS].coverage is Coverage.COMPLETE


def test_a_series_that_stopped_as_the_window_began_leaves_its_host_incomplete() -> None:
    workspace = clean()
    # A second engine index counted up to the scrape before the window, and was not seen again.
    workspace.add(OUTPUT, lambda _: 70, tuple(at for at in SCRAPES if at < START), instance=HOST_A, engine="1")
    read = reading(workspace)

    output = read.hosts[0].counters[HostMetric.OUTPUT_TOKENS_TOTAL]
    assert (output.increase, output.coverage) == (3_000, Coverage.INCOMPLETE)
    assert read.reasons == (Reason.INCOMPLETE_COVERAGE,)


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


def test_a_hole_in_a_hosts_release_samples_leaves_its_membership_unestablished() -> None:
    workspace = clean()
    # Three scrapes of the release metric never arrived: a minute nothing shows the host in.
    workspace.find(RELEASE, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if not 1_108 < at < 1_168)
    read = reading(workspace)

    assert read.hosts[0].membership is Reason.INCOMPLETE_COVERAGE
    assert read.treatments[Treatment.HOSTS] is Reason.INCOMPLETE_COVERAGE
    assert read.treatments[Treatment.RELEASE] is Reason.INCOMPLETE_COVERAGE
    assert read.treatments[Treatment.LAUNCH_SETTINGS] is None
    assert read.reasons == (Reason.INCOMPLETE_COVERAGE,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
    # Nothing says the host left: it is not a turnover, and its metrics are kept.
    assert read.hosts[0].counters[HostMetric.OUTPUT_TOKENS_TOTAL].increase == 3_000


def test_an_engine_that_worked_with_no_release_series_is_a_host_of_unknown_release() -> None:
    workspace = clean()
    add_host(workspace, HOST_C, 30)
    workspace.drop(RELEASE, instance=HOST_C)
    read = reading(workspace)

    assert [host.membership for host in read.hosts] == [None, None, Reason.NOT_OBSERVED]
    assert read.observed.host_count == 3
    assert read.treatments[Treatment.HOSTS] is Reason.NOT_OBSERVED
    assert read.treatments[Treatment.RELEASE] is Reason.NOT_OBSERVED
    assert (read.observed.engine_image_digest, read.observed.instance_type) == (None, None)
    assert read.reasons == (Reason.NOT_OBSERVED,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
    # Its work is the pool's all the same.
    assert read.hosts[2].counters[HostMetric.OUTPUT_TOKENS_TOTAL].increase == 9_000
    assert read.pool.counters[HostMetric.OUTPUT_TOKENS_TOTAL] == 18_000


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


def test_an_identity_first_seen_after_the_gateways_closing_scrape_is_not_the_windows() -> None:
    workspace = clean()
    # The task is scraped on the drain, and someone's first request shows 18 seconds later.
    for counter in workspace.find(GATEWAY):
        counter.times = tuple(sorted((*counter.times, END)))
    workspace.add(GATEWAY, lambda _: 1, (END + 18,), alias="late-smoke", instance=TASK)
    read = reading(workspace)
    assert (read.traffic.pool_requests, read.traffic.external_requests) == (40, 0)
    assert (read.traffic.reconciled, read.reasons) == (True, ())

    # With no scrape on the drain, the one at 1303 closes the window: a request it is the first
    # to show is between the scrapes that bound the window, and overlaps.
    workspace = clean()
    workspace.add(GATEWAY, lambda _: 1, (END + 3, END + 18), alias="late-smoke", instance=TASK)
    read = reading(workspace)
    assert (read.traffic.external_requests, read.reasons) == (1, (Reason.EXTERNAL_TRAFFIC,))


def test_a_request_first_seen_after_the_closing_scrape_does_not_reconcile_the_window() -> None:
    def missed_the_drain(after: int) -> telemetry.Reading:
        # The task is scraped on the drain. The first identity is in every scrape but that one:
        # it is done 30 seconds before the drain, and sends `after` requests two seconds after
        # it, which its next sample is the first to show.
        workspace = clean()
        times = tuple(START + offset for offset in range(-30, 331, 15))
        first, second = workspace.find(GATEWAY)
        second.times = times
        first.times = tuple(at for at in times if at != END)
        first.value = lambda at: (
            3 + int((24 - after) * within(at, START, END - 30) / (END - 30 - START)) + after * (at >= END + 2)
        )
        return reading(workspace)

    # 39 requests of the window and one after it: the later sample says 40, and of when is unknown.
    read = missed_the_drain(1)
    assert not read.traffic.reconciled
    assert read.traffic.coverage is Coverage.INCOMPLETE
    assert Reason.GATEWAY_COVERAGE_INCOMPLETE in read.reasons
    assert InvalidFlag.MISSING_EVIDENCE in read.flags

    # A later sample that says what the series said last shows nothing happened in between.
    read = missed_the_drain(0)
    assert (read.traffic.cohort_requests, read.traffic.reconciled, read.reasons) == (40, True, ())


def test_gateway_samples_that_are_no_numbers_are_missing_evidence_and_not_counted() -> None:
    workspace = clean()
    workspace.find(GATEWAY, alias=COHORT[0])[0].value = lambda _: float("inf")
    read = reading(workspace)

    # The other identity's 16 requests are kept; the first one's count is unknown, not zero.
    assert (read.traffic.cohort_requests, read.traffic.coverage) == (16, Coverage.INCOMPLETE)
    assert not read.traffic.reconciled
    assert read.reasons == (Reason.GATEWAY_COVERAGE_INCOMPLETE, Reason.UNRECONCILED_COUNTERS)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE, InvalidFlag.UNRECONCILED_COUNTERS)
    assert increases(read, HostMetric.OUTPUT_TOKENS_TOTAL) == [3_000, 6_000]


def with_probe(first_alias: int) -> Workspace:
    """The clean workspace with `first_alias` requests of the first identity in the window, and
    a setup probe it sent at 995: after the scrape at 988, so in the first scrape of the window."""
    workspace = clean()
    workspace.find(GATEWAY, alias=COHORT[0])[0].value = lambda at: requests(first_alias, base=3)(at) + (at >= 995)
    return workspace


def test_setup_probes_are_in_neither_the_cohorts_count_nor_the_pools() -> None:
    # The clean cohort sent 3 probes well before the window: the scrape before it holds them.
    settled = reading(clean()).traffic
    assert (settled.pool_requests, settled.cohort_requests, settled.setup_probes_excluded) == (40, 40, 0)

    probed = replace(DRIVER, setup_probes=1, setup_from=995.0)
    read = reading(with_probe(24), driver=probed)
    assert (read.traffic.pool_requests, read.traffic.cohort_requests) == (40, 40)
    assert (read.traffic.setup_probes_excluded, read.traffic.setup_probes_placed) == (1, True)
    assert (read.traffic.external_requests, read.traffic.reconciled, read.flags) == (0, True, ())

    # The driver names it, or the counters do not reconcile.
    unnamed = reading(with_probe(24))
    assert (unnamed.traffic.cohort_requests, unnamed.reasons) == (41, (Reason.UNRECONCILED_COUNTERS,))


def test_a_setup_probe_does_not_stand_in_for_an_attempt_that_never_arrived() -> None:
    # 39 requests of the window and the probe: 40 counted, which is what the driver sent.
    read = reading(with_probe(23), driver=replace(DRIVER, setup_probes=1, setup_from=995.0))

    assert (read.traffic.pool_requests, read.traffic.cohort_requests, read.traffic.driver_requests) == (39, 39, 40)
    assert not read.traffic.reconciled
    assert read.reasons == (Reason.UNRECONCILED_COUNTERS,)
    assert read.flags == (InvalidFlag.UNRECONCILED_COUNTERS,)


def test_a_setup_probe_the_scrapes_cannot_place_leaves_the_counts_unreconciled() -> None:
    # The probes were sent from 980 on, and the scrape at 988 may hold some of them.
    early = reading(with_probe(24), driver=replace(DRIVER, setup_probes=1, setup_from=980.0))
    # The driver gives no time at all.
    untimed = reading(with_probe(24), driver=replace(DRIVER, setup_probes=1))

    for read in (early, untimed):
        assert (read.traffic.pool_requests, read.traffic.cohort_requests) == (40, 40)
        assert not read.traffic.setup_probes_placed
        assert not read.traffic.reconciled
        assert read.reasons == (Reason.SETUP_PROBES_UNPLACED,)
        assert read.flags == (InvalidFlag.UNRECONCILED_COUNTERS,)


def test_matching_counts_over_a_gateway_that_stopped_reporting_do_not_reconcile() -> None:
    workspace = clean()
    # The cohort is done by a third of the window, and no gateway scrape arrives after 1138.
    for counter in workspace.find(GATEWAY):
        whole = counter.value
        counter.value = lambda at, whole=whole: whole(min(END, START + 3 * (at - START)))
        counter.times = tuple(at for at in SCRAPES if at < 1_150)
    read = reading(workspace)

    # What was counted is kept: all 40 requests, and nobody else seen while the gateway reported.
    assert (read.traffic.cohort_requests, read.traffic.external_requests) == (40, 0)
    assert (read.traffic.coverage, read.traffic.reconciled) == (Coverage.INCOMPLETE, False)
    assert read.reasons == (Reason.GATEWAY_COVERAGE_INCOMPLETE,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)


def test_an_identity_whose_series_begins_in_the_window_counts_from_its_first_request() -> None:
    workspace = clean()
    # The gateway serves no series of an identity before its first request.
    workspace.add(GATEWAY, requests(3), tuple(at for at in SCRAPES if at > 1_100), alias="deploy-smoke", instance=TASK)
    read = reading(workspace)

    assert (read.traffic.external_requests, read.traffic.coverage) == (3, Coverage.COMPLETE)
    assert read.reasons == (Reason.EXTERNAL_TRAFFIC,)


def test_an_identity_there_before_the_window_with_no_number_to_count_from_does_not_reconcile() -> None:
    assert reading(clean()).traffic.reconciled

    # The first identity of the cohort first appears in the window: its 24 requests count from zero.
    workspace = clean()
    first = workspace.find(GATEWAY, alias=COHORT[0])[0]
    first.value, first.times = requests(24), tuple(at for at in SCRAPES if at > START)
    read = reading(workspace)
    assert (read.traffic.cohort_requests, read.traffic.reconciled, read.reasons) == (40, True, ())

    # It was scraped before the window, and reads no number until a minute into it, then 24.
    workspace = clean()
    workspace.find(GATEWAY, alias=COHORT[0])[0].value = lambda at: float("nan") if at < START + 60 else 24
    read = reading(workspace)
    assert (read.traffic.coverage, read.traffic.reconciled) == (Coverage.INCOMPLETE, False)
    assert read.reasons == (Reason.GATEWAY_COVERAGE_INCOMPLETE, Reason.UNRECONCILED_COUNTERS)
    answers = telemetry.read(workspace, POOL, VLLM, WINDOW, WARM_UP)
    assert telemetry.identity_requests(answers, WINDOW, COHORT) == {COHORT[0]: None, COHORT[1]: 16}


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
    # 5 tokens a second for the 120 seconds of the warm-up, to the scrape that closes it.
    assert [(host.warm_up_output_tokens, host.warmed) for host in read.hosts] == [(600, True), (600, True)]
    assert {host.warm_up_coverage for host in read.hosts} == {Coverage.COMPLETE}
    assert read.hosts[0].cache == Cache(
        capacity_tokens=400_000, usage_at_start=0.25, usage_peak=0.5, coverage=Coverage.COMPLETE
    )
    assert read.hosts[0].counters[HostMetric.CACHED_INPUT_TOKENS_TOTAL].increase == 15_000


def test_a_preflight_reads_each_hosts_latest_launch_sample_as_its_provenance() -> None:
    # The first host's agent says the fingerprint up to the scrape at 973, and none from 988 on.
    workspace = clean()
    workspace.find(LAUNCH, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if at <= 973)
    workspace.add(
        LAUNCH,
        lambda _: 1,
        tuple(at for at in SCRAPES if at > 973),
        instance=HOST_A,
        settings_fingerprint="unavailable",
    )
    assert telemetry.preflight(workspace, POOL, VLLM, at=1_000).missing == (Source.LAUNCH,)
    assert telemetry.preflight(workspace, POOL, VLLM, at=980).missing == ()


def test_an_engine_named_only_inside_the_window_is_not_warmed_by_the_warm_up() -> None:
    # The first host's agent says no fingerprint through the warm-up, and names its engine at the
    # window's first scrape, 3 seconds in.
    workspace = clean()
    workspace.find(LAUNCH, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if at > START)
    workspace.add(
        LAUNCH,
        lambda _: 1,
        tuple(at for at in SCRAPES if at < START),
        instance=HOST_A,
        settings_fingerprint="unavailable",
    )
    read = reading(workspace)
    first, second = read.hosts

    # Its warm-up's tokens are kept, and nothing ties them to the engine the window measured.
    assert (first.warm_up_output_tokens, first.engine_replaced, first.warmed) == (600, False, False)
    assert (second.warm_up_output_tokens, second.warmed) == (600, True)
    assert Reason.WARM_UP_INCOMPLETE in read.reasons


def test_a_host_that_only_answered_its_own_liveness_was_not_warmed_up() -> None:
    workspace = clean()
    workspace.find(OUTPUT, instance=HOST_B)[0].value = tokens(20, warm=0, liveness=(850.0, 910.0))
    read = reading(workspace)

    assert [(host.warm_up_output_tokens, host.warmed) for host in read.hosts] == [(600, True), (2, False)]
    assert read.reasons == (Reason.WARM_UP_INCOMPLETE,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
    assert reading(clean(), driver=replace(DRIVER, warm_up=None)).reasons == (Reason.WARM_UP_INCOMPLETE,)


def test_the_warm_up_of_an_engine_replaced_before_the_window_does_not_warm_its_replacement() -> None:
    def replaced_at_970(*, fingerprint: bool, counter: bool) -> telemetry.Reading:
        workspace = clean()
        if fingerprint:
            workspace.find(LAUNCH, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if at >= 970)
            workspace.add(
                LAUNCH,
                lambda _: 1,
                tuple(at for at in SCRAPES if at < 970),
                instance=HOST_A,
                settings_fingerprint=OTHER_FINGERPRINT,
            )
        if counter:
            # The new engine counts from zero, and generates nothing before the window.
            output = workspace.find(OUTPUT, instance=HOST_A)[0]
            before = output.value
            output.value = lambda at: before(at) if at < 970 else 10 * within(at, START, END)
        return reading(workspace)

    for read in (
        replaced_at_970(fingerprint=True, counter=True),
        replaced_at_970(fingerprint=True, counter=False),
        replaced_at_970(fingerprint=False, counter=True),
    ):
        first, second = read.hosts
        # What the earlier engine counted is kept, and warmed nothing the window measured.
        assert (first.warm_up_output_tokens, first.engine_replaced, first.warmed) == (600, True, False)
        assert (second.engine_replaced, second.warmed) == (False, True)
        assert read.reasons == (Reason.WARM_UP_ENGINE_REPLACED,)
        assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
        # The window itself had one engine, of the requested fingerprint, throughout.
        assert read.unverified == ()
        assert first.counters[HostMetric.OUTPUT_TOKENS_TOTAL].increase == 3_000


def test_an_agent_that_names_its_engine_during_the_warm_up_names_no_engine_before() -> None:
    def named_from_850(work: tuple[float, float]) -> telemetry.Host:
        # The agent of the first host says no fingerprint to 850, and the requested one from then
        # on. Its engine counts 300 tokens between the two times of `work`, with no restart.
        workspace = clean()
        workspace.find(LAUNCH, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if at >= 850)
        workspace.add(
            LAUNCH,
            lambda _: 1,
            tuple(at for at in SCRAPES if at < 850),
            instance=HOST_A,
            settings_fingerprint="unavailable",
        )
        workspace.find(OUTPUT, instance=HOST_A)[0].value = lambda at: (
            50 + 300 * within(at, *work) / (work[1] - work[0]) + 10 * within(at, START, END)
        )
        read = reading(workspace)
        assert read.unverified == ()
        return read.hosts[0]

    host = named_from_850((870.0, 930.0))
    assert (host.warm_up_output_tokens, host.engine_replaced, host.warmed) == (300, False, True)

    # Work done before the agent named the engine is kept, and is not known to be that engine's.
    host = named_from_850((822.0, 836.0))
    assert (host.warm_up_output_tokens, host.engine_replaced, host.warmed) == (300, False, False)


def test_work_the_warm_up_and_the_window_both_count_leaves_the_window_unqualified() -> None:
    # The warm-up runs to the window's start: the scrapes at 988 and 1003 bound the end of the
    # one and the start of the other. What a busy pool adds in the window's first three seconds
    # is in that rise.
    driver = replace(DRIVER, warm_up=Window(WARM_UP.started, START))
    assert reading(clean(), driver=driver).reasons == (Reason.WARM_UP_OVERLAPS,)
    # A warm-up that was over by the scrape at 988 shares no rise with the window.
    assert reading(clean(), driver=replace(DRIVER, warm_up=Window(WARM_UP.started, 988.0))).reasons == ()

    # Engines that count nothing in the window: no counter rises between the two scrapes.
    workspace = clean()
    for counter in (OUTPUT, INPUT, CACHED, BY_SOURCE):
        for one in workspace.find(counter):
            one.value = tokens(0)
    read = reading(workspace, driver=driver)
    assert (read.reasons, [host.warm_up_overlaps for host in read.hosts]) == ((), [False, False])

    # The first host ends its warm-up with 100 tokens at 995, and generates nothing in the window.
    workspace.find(OUTPUT, instance=HOST_A)[0].value = lambda at: tokens(0)(at) + 100 * (at >= 995)
    read = reading(workspace, driver=driver)
    first, second = read.hosts
    # The window's increase holds them, and is marked as holding what may be the warm-up's.
    assert first.counters[HostMetric.OUTPUT_TOKENS_TOTAL].increase == 100
    assert (first.warm_up_overlaps, second.warm_up_overlaps) == (True, False)
    assert read.reasons == (Reason.WARM_UP_OVERLAPS,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
    assert (first.warm_up_output_tokens, first.warmed) == (700, True)

    # Tokens the window's scrapes count too are not shown to be the warm-up's: they warm nothing.
    workspace.find(OUTPUT, instance=HOST_A)[0].value = lambda at: tokens(0, warm=0)(at) + 100 * (at >= 995)
    first = reading(workspace, driver=driver).hosts[0]
    assert (first.warm_up_output_tokens, first.warm_up_overlaps, first.warmed) == (100, True, False)


def test_a_warm_up_is_shown_by_a_rise_its_scrapes_cover() -> None:
    # The first sample of the counter, after the warm-up began, holds 500 tokens, and it never
    # rises during the warm-up: a value is not work.
    workspace = clean()
    output = workspace.find(OUTPUT, instance=HOST_A)[0]
    output.value = tokens(10, warm=0, base=500)
    output.times = tuple(at for at in SCRAPES if at > WARM_UP.started)
    read = reading(workspace)
    first = read.hosts[0]
    assert (first.warm_up_output_tokens, first.warm_up_coverage, first.warmed) == (0, Coverage.INCOMPLETE, False)
    assert read.reasons == (Reason.WARM_UP_INCOMPLETE,)

    # 100 tokens between the last scrape of the warm-up and its end are in the scrape after it.
    workspace = clean()
    workspace.find(OUTPUT, instance=HOST_A)[0].value = lambda at: tokens(10, warm=0)(at) + 100 * (at >= 935)
    first = reading(workspace).hosts[0]
    assert (first.warm_up_output_tokens, first.warmed) == (100, True)


def test_a_warm_up_needs_every_required_series_of_the_host_through_it() -> None:
    workspace = clean()
    # The preemptions of the first host are scraped from the end of the warm-up on only.
    workspace.find(PREEMPTIONS, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if at > WARM_UP.drained)
    read = reading(workspace)

    assert [(host.warm_up_coverage, host.warmed) for host in read.hosts] == [
        (Coverage.INCOMPLETE, False),
        (Coverage.COMPLETE, True),
    ]
    # The window's own preemption coverage is whole: only the warm-up lacks evidence.
    assert read.hosts[0].counters[HostMetric.PREEMPTIONS_TOTAL].coverage is Coverage.COMPLETE
    assert read.reasons == (Reason.WARM_UP_INCOMPLETE,)
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)


def test_cache_usage_that_stops_being_reported_is_incomplete_cache_evidence() -> None:
    workspace = clean()
    workspace.find(CACHE_USAGE, instance=HOST_A)[0].times = tuple(at for at in SCRAPES if at <= START + 6)
    read = reading(workspace)

    cache = read.hosts[0].cache
    # What its one scrape of the window said is kept, and is not the window's peak for certain.
    assert (cache.usage_at_start, cache.usage_peak, cache.coverage) == (0.25, 0.25, Coverage.INCOMPLETE)
    assert read.hosts[1].cache.coverage is Coverage.COMPLETE
    assert read.reasons == (Reason.INCOMPLETE_COVERAGE,)


def test_missing_cache_and_preemption_evidence_shows_in_the_preflight_and_the_reading() -> None:
    workspace = clean()
    workspace.drop(CACHE_CAPACITY, instance=HOST_A)
    workspace.drop(PREEMPTIONS, instance=HOST_B)

    assert telemetry.preflight(workspace, POOL, VLLM, at=START).missing == (
        Source.CACHE_CAPACITY,
        HostMetric.PREEMPTIONS_TOTAL,
    )
    # A series whose samples are no numbers reports nothing either.
    unreadable = clean()
    unreadable.find(PREEMPTIONS, instance=HOST_A)[0].value = lambda _: float("nan")
    assert telemetry.preflight(unreadable, POOL, VLLM, at=START).missing == (HostMetric.PREEMPTIONS_TOTAL,)
    assert Reason.PREEMPTION_COVERAGE_MISSING in reading(unreadable).reasons

    read = reading(workspace)
    assert read.hosts[0].cache.capacity_tokens is None
    assert set(read.reasons) == {
        Reason.PREEMPTION_COVERAGE_MISSING,
        Reason.CACHE_EVIDENCE_MISSING,
        Reason.WARM_UP_INCOMPLETE,
    }
    assert read.flags == (InvalidFlag.MISSING_EVIDENCE,)
