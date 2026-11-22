"""The inference performance dashboard: what each pool serves, measured beside what was predicted.

One of the six dashboards of the system design (section 7.3), for requirement R9. One row for
each pool of the configuration, of the panels of section 7.2's performance table:

| Panel | From the engine | From the gateway |
| --- | --- | --- |
| Output speed per stream | The mean, output tokens over decode time (the headline); the median and the slowest tenth, from the time per output token; each instance's computed-input rate, which is what slows a stream | The median and the slowest tenth of each stream's tokens a second |
| Output throughput | Output tokens a second, for the pool and for each instance | Output tokens settled a second |
| Time to first token | The median and the 95th percentile, and those of its two parts, queue time and prefill time | The same, by start (warm or held) and turn (first or later), to the first token and to the first token that is not reasoning |
| Input throughput | Input tokens a second, cached ones included, and the part computed beside it | Input tokens settled a second |
| Prefill speed | Tokens computed over prefill time | None |
| Transcript size and output per turn | The median and the 95th percentile of each request's input and output tokens | The same, from the tokens settled |
| Cached share of input | Cached input tokens over input tokens | None |
| Cache use against capacity | Each instance's cache use and preemptions; the capacity in tokens the lifecycle agent exports, beside the pool's expected capacity | None |
| Sessions the cache forgot | The median and the 95th percentile of how long a block sat idle before it was evicted, and the blocks evicted | None |
| Concurrent streams and the queue | Each instance's running and waiting requests | The requests in flight and held, summed over the gateway's tasks |
| Students per instance, an average | None | The students with a request in the last five minutes, over the scaler's instances in service |
| Cost per million output tokens, estimate | None | The cost dashboard's estimated dollars over the gateway's output tokens, over the dashboard's range; linked to the month's, on the cost dashboard |
| Tokens per GPU-hour and per joule | Output throughput over the scaler's instances in service, and over the rate of the DCGM exporter's energy counter | None |

**What a pool's engine does not give is not drawn.** An engine's names come from the per-engine
table, at the row of the pool's engine (`rhi_common.engine_metrics`). The row names every quantity
of the contract, and may leave out any further one (the computed input, the queue, prefill and
decode times, each request's tokens, preemptions, how long a block sat idle before it was evicted):
the mock's leaves out all of them. A series that reads a quantity the row leaves out is not drawn,
and a panel left with no measured series is not shown. So the mock pool's row has no prefill-speed
panel and no panel on the sessions the cache forgot, and its cache panel has no preemptions. A
reference line is not a measured series. A mock instance has no GPU: a pool whose engine is the
mock has no panel on tokens per GPU-hour and per joule.

**The prediction is on the panel.** A pool's `expected` values (section 4.7) are drawn as
reference lines: the low and the high bound of its speed per stream on the speed panel, those of
its time to first token on the first-token panel. The objectives' floors (section 7.3) are drawn
there too, from the configuration: the median stream's and the slowest tenth's speed, and the
first token's 95th percentile. The cache panel draws the low and the high bound of its expected
cache capacity in tokens (`cache_tokens`) beside the capacity each instance's lifecycle agent
reports (`rhi_lifecycle.cache`), on the same axis. A pool that gives no expected values shows only
the floors, and the reported capacity alone. The cached-share panel turns red below the configured
share, 85% by default.

**Students are counted once.** The gateway's per-student counter of requests has one series for
each alias and each gateway task (the collector labels a task's series with its ID): the query
first takes, for each series, whether it counted a request in the last five minutes (the last
sample it reported in the window differs from the last it reported in the five minutes before, or
it reported none then), then counts the pool's
aliases among those, so two tasks that served one student count one. It reads zero while the
scaler reports the pool and no student is at work. Over the instances in service it is an average:
the gateway cannot know which instance serves a student (section 5.5). No series drawn carries an
alias.

**The cost is the cost dashboard's** (`rhi_dashboards.cost`): Auto Scaling's count of the pool's
running instances, warm-pool preparation included, summed over the window and times the pool's
configured hourly price, over the output tokens the gateway settled for the pool in that same
window, in millions. It is an estimate, and its title says so. The panel reads the dashboard's
range, a past session's fixed dates included, as the cost dashboard's companion does. The month's
estimate, from the start of the month to now, in UTC, as AWS bills, is the cost dashboard's own,
and the panel links to it: CloudWatch answers only inside a panel's range, and on fixed dates
Grafana gives every panel the dashboard's range, whatever relative time the panel asks for, so no
panel of a dashboard with a time picker can hold the month. The dashboard is in UTC, as that one.

Every panel follows the rules of `rhi_dashboards.rules`: a rate is taken per series and then
summed, a percentile comes with the count of requests behind it over the same window and labels,
and an engine series is kept to the pool in service, the panel reading "asleep" without one. So
is every series of an instance: the capacity its lifecycle agent reports, its GPUs' energy. The
gateway's series are never drawn by student, and are drawn as they are: the gateway runs whether
or not the pool has an instance.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from grafana_foundation_sdk.builders import common as common_builders
from grafana_foundation_sdk.builders import dashboard as dashboard_builders
from grafana_foundation_sdk.models import common
from grafana_foundation_sdk.models import dashboard as models

from rhi_common.engine_metrics import MOCK, Further, Quantity
from rhi_dashboards import cost, gpu, rules

if TYPE_CHECKING:
    from grafana_foundation_sdk.builders import stat as stat_builders
    from grafana_foundation_sdk.builders import timeseries as timeseries_builders

    from rhi.config import Config
    from rhi_common.pool_config import Pool

UID = "inference"
TITLE = "Inference performance"

# The gateway's series of what students experience, by pool and wire format, never by student.
GATEWAY = "services/gateway/rhi_gateway/experience.py"
GATEWAY_SPEED = "rhi_gateway_stream_output_tokens_per_second"
GATEWAY_INPUT = "rhi_gateway_request_input_tokens"
GATEWAY_OUTPUT = "rhi_gateway_request_output_tokens"
GATEWAY_FIRST_TOKEN = "rhi_gateway_time_to_first_token_seconds"
GATEWAY_FIRST_ANSWER = "rhi_gateway_time_to_first_answer_seconds"
GATEWAY_IN_FLIGHT = "rhi_gateway_requests_in_flight"
GATEWAY_HELD = "rhi_gateway_requests_held"
# The gateway's per-student counter of requests, by alias and pool: read only to count the aliases.
GATEWAY_STUDENTS = "services/gateway/rhi_gateway/metrics.py"
STUDENT_REQUESTS = "rhi_gateway_student_requests"
ALIAS = "alias"
# A student with a request in this window is one at work.
STUDENT_WINDOW = "5m"
# The cache capacity in tokens each instance's lifecycle agent exports, a gauge.
LIFECYCLE_CACHE = "services/engine/lifecycle/rhi_lifecycle/cache.py"
CACHE_CAPACITY = "rhi_lifecycle_engine_cache_capacity_tokens"
# The labels of the gateway's two waits: warm or held, and first turn or later.
WAIT_LABELS = ("start", "turn")
# The label the collector gives every series of an instance: the instance's ID.
INSTANCE = "instance"

# The panels' titles, in the row's order.
SPEED = "Output speed per stream: tokens a second"
THROUGHPUT = "Output throughput: tokens a second, per pool and per instance"
FIRST_TOKEN = "Time to first token: seconds"
INPUT = "Input throughput: tokens a second, and the part computed"
PREFILL = "Prefill speed: tokens computed a second"
TRANSCRIPT = "Transcript size and output per turn: tokens"
CACHED_SHARE = "Cached share of input"
CACHE = "Cache use against capacity, per instance"
FORGOT = "Sessions the cache forgot: seconds a block sat idle before it was evicted"
STREAMS = "Concurrent streams and the queue: requests"
STUDENTS = "Students per instance, an average: students with a request in the last five minutes"
COST = "Cost per million output tokens, estimate: the dashboard's range"
PER_GPU = "Tokens per GPU-hour and per joule"
TITLES = (
    SPEED,
    THROUGHPUT,
    FIRST_TOKEN,
    INPUT,
    PREFILL,
    TRANSCRIPT,
    CACHED_SHARE,
    CACHE,
    FORGOT,
    STREAMS,
    STUDENTS,
    COST,
    PER_GPU,
)
# The cost panel's link to the month's estimate, which the cost dashboard holds.
COST_MONTH_LINK = "Cost per million output tokens, this month, estimate: the Cost dashboard"
COST_MONTH_URL = f"/d/{cost.UID}"

# The legends of the reference lines. Only the expected ones start with EXPECTED.
EXPECTED = "expected"
EXPECTED_LOW = f"{EXPECTED} low"
EXPECTED_HIGH = f"{EXPECTED} high"
FLOOR_MEDIAN = "floor: median stream"
FLOOR_SLOWEST_TENTH = "floor: slowest tenth"
FLOOR_FIRST_TOKEN = "floor: 95th percentile"
REPORTED_CAPACITY = f"reported capacity {{{{{INSTANCE}}}}}"
# The count behind the idle times' percentiles: one observation for each block evicted.
EVICTED_BLOCKS = "evicted blocks"

# The units of the series read on the right-hand axis. Grafana gives each unit its own scale.
TOKENS = "short"
PREEMPTIONS_A_SECOND = "reqps"
TOKENS_A_JOULE = "suffix: tokens/J"
SECONDS_AN_HOUR = 3600
# The cost dashboard's time zone, UTC, as AWS bills.
TIME_ZONE = cost.TIME_ZONE

METRICS: tuple[rules.Declared, ...] = (
    *(
        rules.FromEngineTable(quantity)
        for quantity in (
            Quantity.OUTPUT_TOKENS,
            Quantity.INPUT_TOKENS,
            Quantity.CACHED_INPUT_TOKENS,
            Quantity.TIME_TO_FIRST_TOKEN,
            Quantity.TIME_PER_OUTPUT_TOKEN,
            Further.COMPUTED_INPUT_TOKENS,
            Further.QUEUE_TIME,
            Further.PREFILL_TIME,
            Further.DECODE_TIME,
            Further.REQUEST_INPUT_TOKENS,
            Further.REQUEST_OUTPUT_TOKENS,
            Further.REQUEST_PREFILL_COMPUTED_TOKENS,
            Quantity.CACHE_USAGE,
            Quantity.RUNNING_REQUESTS,
            Quantity.WAITING_REQUESTS,
            Further.PREEMPTIONS,
            Further.BLOCK_IDLE_BEFORE_EVICTION,
        )
    ),
    *(
        rules.EmittedBy(name, GATEWAY)
        for name in (
            GATEWAY_SPEED,
            GATEWAY_INPUT,
            GATEWAY_OUTPUT,
            GATEWAY_FIRST_TOKEN,
            GATEWAY_FIRST_ANSWER,
            GATEWAY_IN_FLIGHT,
            GATEWAY_HELD,
        )
    ),
    rules.EmittedBy(STUDENT_REQUESTS, GATEWAY_STUDENTS),
    rules.EmittedBy(CACHE_CAPACITY, LIFECYCLE_CACHE),
    rules.EmittedBy(gpu.ENERGY, gpu.COUNTERS),
    *cost.METRICS,
)

# The colour of the cached-share panel's "asleep": neither the flag's red nor its green.
ASLEEP_COLOR = "grey"

# A row is 24 columns wide: two panels to a line.
HALF = 12


def _reports(pool: Pool, *quantities: rules.EngineQuantity) -> bool:
    """Whether the row of the pool's engine names every one of the quantities."""
    return all(rules.engine_reports(pool, quantity) for quantity in quantities)


def _engine(legend: str, expression: str, axis: str | None = None) -> rules.Series:
    return rules.Series(legend, expression, engine=True, axis=axis)


def _line(legend: str, value: float, axis: str | None = None) -> rules.Series:
    """A reference line: the same value at every moment, written in full, never rounded."""
    number = float(value)
    written = str(int(number)) if number.is_integer() else repr(number)
    return rules.Series(legend, f"vector({written})", axis=axis)


def _expected(bounds: tuple[float, float] | None, axis: str | None = None) -> list[rules.Series]:
    """The lines of an expected value's low and high bound, or none when the pool gives none."""
    if bounds is None:
        return []
    low, high = bounds
    return [_line(EXPECTED_LOW, low, axis), _line(EXPECTED_HIGH, high, axis)]


def _ratio(pool: Pool, numerator: rules.EngineQuantity, denominator: rules.EngineQuantity, sample: str = "") -> str:
    """One sum of rates over another, both for the pool: a mean that needs no buckets."""
    return f"{rules.engine_rate(pool, numerator, sample)} / {rules.engine_rate(pool, denominator, sample)}"


def _gateway(pool: Pool) -> dict[str, str]:
    return {rules.POOL: pool.name}


def _per_instance(selector: str, aggregation: str = "max") -> str:
    """A gauge's one series for each instance of the pool, whatever other label it carries."""
    return f"{aggregation} by ({rules.POOL}, {INSTANCE}) ({selector})"


def _in_service(pool: Pool) -> str:
    """The scaler's count of the pool's instances in service, kept where it is above zero."""
    return f"(sum by ({rules.POOL}) ({rules.in_service(pool.name)}) > 0)"


def speed(pool: Pool, config: Config) -> tuple[list[rules.Series], list[rules.Series]]:
    """The speed panel's measured series and its reference lines."""
    measured: list[rules.Series] = []
    if _reports(pool, Further.REQUEST_OUTPUT_TOKENS, Further.DECODE_TIME):
        measured.append(
            _engine("engine mean", _ratio(pool, Further.REQUEST_OUTPUT_TOKENS, Further.DECODE_TIME, "_sum"))
        )
    # A stream's speed is one over its time per output token: the slowest tenth is the 90th
    # percentile of that time.
    tpot = Quantity.TIME_PER_OUTPUT_TOKEN
    measured += [
        _engine("engine median", f"1 / {rules.engine_quantile(pool, 0.5, tpot)}"),
        _engine("engine slowest tenth", f"1 / {rules.engine_quantile(pool, 0.9, tpot)}"),
        _engine("engine requests", rules.engine_request_count(pool, tpot), axis="short"),
    ]
    measured += rules.percentile_series(GATEWAY_SPEED, _gateway(pool), quantiles=(0.5, 0.1), legend="gateway ")
    if _reports(pool, Further.COMPUTED_INPUT_TOKENS):
        computed = rules.engine_rate(pool, Further.COMPUTED_INPUT_TOKENS, by=(INSTANCE,))
        measured.append(_engine(f"computed input {{{{{INSTANCE}}}}}", computed, axis="short"))
    expected = pool.expected.stream_tokens_per_second if pool.expected else None
    references = [
        *_expected(expected),
        _line(FLOOR_MEDIAN, config.target_stream_median_tokens_per_second),
        _line(FLOOR_SLOWEST_TENTH, config.target_stream_p10_tokens_per_second),
    ]
    return measured, references


def throughput(pool: Pool) -> list[rules.Series]:
    """The output tokens a second, for the pool and for each instance, and those the gateway settled."""
    return [
        _engine("engine", rules.engine_rate(pool, Quantity.OUTPUT_TOKENS)),
        _engine(f"engine {{{{{INSTANCE}}}}}", rules.engine_rate(pool, Quantity.OUTPUT_TOKENS, by=(INSTANCE,))),
        rules.Series("gateway settled", rules.rate(f"{GATEWAY_OUTPUT}_sum", _gateway(pool))),
    ]


def first_token(pool: Pool, config: Config) -> tuple[list[rules.Series], list[rules.Series]]:
    """The first-token panel's measured series and its reference lines."""
    measured = rules.engine_percentile_series(pool, Quantity.TIME_TO_FIRST_TOKEN, legend="engine ")
    for part, legend in ((Further.QUEUE_TIME, "engine queue "), (Further.PREFILL_TIME, "engine prefill ")):
        if _reports(pool, part):
            measured += rules.engine_percentile_series(pool, part, legend=legend)
    waits = " ".join(f"{{{{{label}}}}}" for label in WAIT_LABELS)
    for histogram, legend in ((GATEWAY_FIRST_TOKEN, "gateway"), (GATEWAY_FIRST_ANSWER, "gateway answer")):
        measured += rules.percentile_series(histogram, _gateway(pool), by=WAIT_LABELS, legend=f"{legend} {waits} ")
    expected = pool.expected.time_to_first_token_seconds if pool.expected else None
    references = [*_expected(expected), _line(FLOOR_FIRST_TOKEN, config.target_ttft_p95_seconds)]
    return measured, references


def input_throughput(pool: Pool) -> list[rules.Series]:
    """The input tokens a second, cached ones included, the part computed beside them, and the gateway's."""
    measured = [_engine("engine input", rules.engine_rate(pool, Quantity.INPUT_TOKENS))]
    if _reports(pool, Further.COMPUTED_INPUT_TOKENS):
        measured.append(_engine("engine computed", rules.engine_rate(pool, Further.COMPUTED_INPUT_TOKENS)))
    measured.append(rules.Series("gateway settled input", rules.rate(f"{GATEWAY_INPUT}_sum", _gateway(pool))))
    return measured


def prefill(pool: Pool) -> list[rules.Series]:
    """The tokens computed a second of prefill: none where the engine's row leaves either out."""
    computed, seconds = Further.REQUEST_PREFILL_COMPUTED_TOKENS, Further.PREFILL_TIME
    if not _reports(pool, computed, seconds):
        return []
    return [_engine("engine", _ratio(pool, computed, seconds, "_sum"))]


def transcript(pool: Pool) -> list[rules.Series]:
    """Each request's input tokens, the transcript, and its output tokens, from the engine and the gateway."""
    measured: list[rules.Series] = []
    for quantity, legend in ((Further.REQUEST_INPUT_TOKENS, "transcript"), (Further.REQUEST_OUTPUT_TOKENS, "output")):
        if _reports(pool, quantity):
            measured += rules.engine_percentile_series(pool, quantity, legend=f"engine {legend} ")
    for histogram, legend in ((GATEWAY_INPUT, "transcript"), (GATEWAY_OUTPUT, "output")):
        measured += rules.percentile_series(histogram, _gateway(pool), legend=f"gateway {legend} ")
    return measured


def cached_share(pool: Pool) -> list[rules.Series]:
    """The share of the input read from the cache."""
    return [_engine("cached share", _ratio(pool, Quantity.CACHED_INPUT_TOKENS, Quantity.INPUT_TOKENS))]


def cache(pool: Pool) -> tuple[list[rules.Series], list[rules.Series]]:
    """Each instance's cache use, its preemptions, and its reported capacity beside the expected one."""
    usage = _per_instance(rules.engine_selector(pool, Quantity.CACHE_USAGE))
    measured = [_engine(f"cache use {{{{{INSTANCE}}}}}", usage)]
    if _reports(pool, Further.PREEMPTIONS):
        preempted = rules.engine_rate(pool, Further.PREEMPTIONS, by=(INSTANCE,))
        measured.append(_engine(f"preemptions {{{{{INSTANCE}}}}}", preempted, axis=PREEMPTIONS_A_SECOND))
    capacity = _per_instance(rules.selector(CACHE_CAPACITY, _gateway(pool)))
    measured.append(_engine(REPORTED_CAPACITY, capacity, axis=TOKENS))
    expected = pool.expected.cache_tokens if pool.expected else None
    return measured, _expected(expected, axis=TOKENS)


def forgotten(pool: Pool) -> list[rules.Series]:
    """How long a block of the cache sat idle before it was evicted: none where the engine's row leaves it out."""
    if not _reports(pool, Further.BLOCK_IDLE_BEFORE_EVICTION):
        return []
    found = rules.engine_percentile_series(pool, Further.BLOCK_IDLE_BEFORE_EVICTION, legend="idle before eviction ")
    # The histogram observes blocks, not requests: its count is read as the blocks evicted.
    *percentiles, observed = found
    return [*percentiles, dataclasses.replace(observed, legend=EVICTED_BLOCKS)]


def streams(pool: Pool) -> list[rules.Series]:
    """Each instance's running and waiting requests, and the gateway's in flight and held, summed over its tasks."""
    measured = [
        _engine(f"{legend} {{{{{INSTANCE}}}}}", _per_instance(rules.engine_selector(pool, quantity), "sum"))
        for quantity, legend in ((Quantity.RUNNING_REQUESTS, "running"), (Quantity.WAITING_REQUESTS, "waiting"))
    ]
    measured += [
        rules.Series(legend, f"sum by ({rules.POOL}) ({rules.selector(gauge, _gateway(pool))})")
        for gauge, legend in ((GATEWAY_IN_FLIGHT, "gateway in flight"), (GATEWAY_HELD, "gateway held"))
    ]
    return measured


def active(pool: Pool) -> str:
    """Each series of the pool's per-student counter that counted a request in the last five
    minutes: the last sample it reported in the window differs from the last it reported in the
    window before (so a request scraped just inside the window counts), or it reported none then (a
    series appears holding its first request). Both are read from the samples themselves, not from
    instant values: a series that went stale after its request still counts it, and one that missed
    a scrape at the window's start and came back unchanged counts nothing."""
    series = rules.selector(STUDENT_REQUESTS + rules.COUNTER_SAMPLE, _gateway(pool), matching={ALIAS: ".+"})
    now = f"last_over_time({series}[{STUDENT_WINDOW}])"
    before = f"last_over_time({series}[{STUDENT_WINDOW}] offset {STUDENT_WINDOW})"
    return f"({now} != {before}) or ({now} unless {before})"


def students(pool: Pool) -> str:
    """The pool's aliases with a request in the last five minutes, each counted once, however many
    gateway tasks counted its requests: one per alias of the active series, then the aliases; zero
    while the scaler reports the pool and no alias is active."""
    counted = f"count by ({rules.POOL}) (count by ({rules.POOL}, {ALIAS}) ({active(pool)}))"
    none = f"0 * sum by ({rules.POOL}) ({rules.in_service(pool.name)})"
    return f"({counted}) or ({none})"


def students_per_instance(pool: Pool) -> list[rules.Series]:
    """The students at work over the instances in service: an average, none while none is in service."""
    return [rules.Series("students per instance", f"({students(pool)}) / {_in_service(pool)}")]


def per_gpu(pool: Pool) -> list[rules.Series]:
    """Output tokens per GPU-hour, over the instances in service, and per joule, over the energy the
    GPUs used: none for a pool whose engine is the mock, which has no GPU."""
    if pool.engine == MOCK:
        return []
    output = rules.engine_rate(pool, Quantity.OUTPUT_TOKENS)
    joules = rules.rate(gpu.ENERGY_SERIES, _gateway(pool))
    return [
        _engine("tokens per GPU-hour", f"{output} * {SECONDS_AN_HOUR} / {_in_service(pool)}"),
        _engine("tokens per joule", f"{output} / ({joules} > 0)", axis=TOKENS_A_JOULE),
    ]


def cost_panel(pool: Pool) -> stat_builders.Panel:
    """The cost dashboard's estimated cost per million output tokens over the dashboard's range,
    fixed dates included, linked to the month's estimate on the cost dashboard."""
    description = (
        f"The estimated cost of the pool's instances, at ${cost.price(pool)!r} an hour, over the output "
        "tokens the gateway settled for the pool, in millions, over the dashboard's time range. The "
        "month's, from the start of the month, UTC, to now, is on the Cost dashboard (the panel's link)."
    )
    link = dashboard_builders.DashboardLink(COST_MONTH_LINK).type(models.DashboardLinkType.LINK).url(COST_MONTH_URL)
    return cost.per_million_stat(pool, of_session=True).title(COST).description(description).links([link])


def _flag(panel: timeseries_builders.Panel, threshold: float) -> timeseries_builders.Panel:
    """Red below the threshold, green from it: the line, and the area under it, are drawn.

    The "asleep" series is zero, below any threshold: it is drawn in grey, with no threshold of its
    own, so a pool with no instance does not read as a cache that fails.
    """
    steps = [models.Threshold(value=None, color="red"), models.Threshold(value=threshold, color="green")]
    return (
        panel.thresholds(dashboard_builders.ThresholdsConfig().mode(models.ThresholdsMode.ABSOLUTE).steps(steps))
        .color_scheme(dashboard_builders.FieldColor().mode(models.FieldColorModeId.THRESHOLDS))
        .thresholds_style(
            common_builders.GraphThresholdsStyleConfig().mode(common.GraphThresholdsStyleMode.LINE_AND_AREA)
        )
        .override_by_query(
            rules.ASLEEP_REF,
            [
                models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": ASLEEP_COLOR}),
                models.DynamicConfigValue(
                    id_val="thresholds",
                    value={"mode": "absolute", "steps": [{"value": None, "color": ASLEEP_COLOR}]},
                ),
                models.DynamicConfigValue(id_val="custom.thresholdsStyle", value={"mode": "off"}),
            ],
        )
    )


def panels(pool: Pool, config: Config) -> list[timeseries_builders.Panel | stat_builders.Panel]:
    """The row of the pool: each panel that has a measured series, in the order of `TITLES`.

    Reference lines are drawn beside the measured series, and do not keep a panel on their own.
    """
    speed_series, speed_lines = speed(pool, config)
    first_series, first_lines = first_token(pool, config)
    cache_series, cache_lines = cache(pool)
    rows: list[tuple[str, list[rules.Series], list[rules.Series], str] | stat_builders.Panel] = [
        (SPEED, speed_series, speed_lines, "short"),
        (THROUGHPUT, throughput(pool), [], "short"),
        (FIRST_TOKEN, first_series, first_lines, "s"),
        (INPUT, input_throughput(pool), [], "short"),
        (PREFILL, prefill(pool), [], "short"),
        (TRANSCRIPT, transcript(pool), [], "short"),
        (CACHED_SHARE, cached_share(pool), [], "percentunit"),
        (CACHE, cache_series, cache_lines, "percentunit"),
        (FORGOT, forgotten(pool), [], "s"),
        (STREAMS, streams(pool), [], "short"),
        (STUDENTS, students_per_instance(pool), [], "short"),
        cost_panel(pool),
        (PER_GPU, per_gpu(pool), [], "short"),
    ]
    found: list[timeseries_builders.Panel | stat_builders.Panel] = []
    for row in rows:
        if not isinstance(row, tuple):
            found.append(row)
            continue
        title, measured, references, unit = row
        if not measured:
            continue
        panel = rules.pool_timeseries(pool, title, [*measured, *references]).unit(unit).min(0)
        if title == CACHED_SHARE:
            panel = _flag(panel.max(1), config.cached_share_flag_percent / 100)
        found.append(panel)
    return found


def dashboard(config: Config) -> dashboard_builders.Dashboard:
    """The dashboard, with one row for each pool of the configuration, in its order, in UTC."""
    built = dashboard_builders.Dashboard(TITLE).uid(UID).timezone(TIME_ZONE)
    for pool in config.pools:
        built.with_row(dashboard_builders.Row(pool.name))
        for panel in panels(pool, config):
            built.with_panel(panel.span(HALF))
    return built
