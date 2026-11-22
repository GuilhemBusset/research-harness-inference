"""The inference performance dashboard: what each pool serves, measured beside what was predicted.

One of the six dashboards of the system design (section 7.3), for requirement R9. One row for
each pool of the configuration, of the panels of section 7.2's performance table that are about
speed and latency:

| Panel | From the engine | From the gateway |
| --- | --- | --- |
| Output speed per stream | The mean, output tokens over decode time (the headline); the median and the slowest tenth, from the time per output token; each instance's computed-input rate, which is what slows a stream | The median and the slowest tenth of each stream's tokens a second |
| Output throughput | Output tokens a second, for the pool and for each instance | Output tokens settled a second |
| Time to first token | The median and the 95th percentile, and those of its two parts, queue time and prefill time | The same, by start (warm or held) and turn (first or later), to the first token and to the first token that is not reasoning |
| Input throughput | Input tokens a second, cached ones included, and the part computed beside it | Input tokens settled a second |
| Prefill speed | Tokens computed over prefill time | None |
| Transcript size and output per turn | The median and the 95th percentile of each request's input and output tokens | The same, from the tokens settled |
| Cached share of input | Cached input tokens over input tokens | None |

**What a pool's engine does not give is not drawn.** An engine's names come from the per-engine
table, at the row of the pool's engine (`rhi_common.engine_metrics`). The row names every quantity
of the contract, and may leave out any further one (the computed input, the queue, prefill and
decode times, each request's tokens): the mock's leaves out all of them. A series that reads a
quantity the row leaves out is not drawn, and a panel left with no measured series is not shown.
So the mock pool's row has no prefill-speed panel. A reference line is not a measured series.

**The prediction is on the panel.** A pool's `expected` values (section 4.7) are drawn as
reference lines: the low and the high bound of its speed per stream on the speed panel, those of
its time to first token on the first-token panel. The objectives' floors (section 7.3) are drawn
there too, from the configuration: the median stream's and the slowest tenth's speed, and the
first token's 95th percentile. A pool that gives no expected values shows only the floors. The
cached-share panel turns red below the configured share, 85% by default.

Every panel follows the rules of `rhi_dashboards.rules`: a rate is taken per series and then
summed, a percentile comes with the count of requests behind it over the same window and labels,
and an engine series is kept to the pool in service, the panel reading "asleep" without one. The
gateway's series are never labelled by student, and are drawn as they are: the gateway runs
whether or not the pool has an instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from grafana_foundation_sdk.builders import common as common_builders
from grafana_foundation_sdk.builders import dashboard as dashboard_builders
from grafana_foundation_sdk.models import common
from grafana_foundation_sdk.models import dashboard as models

from rhi_common.engine_metrics import Further, Quantity
from rhi_dashboards import rules

if TYPE_CHECKING:
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
TITLES = (SPEED, THROUGHPUT, FIRST_TOKEN, INPUT, PREFILL, TRANSCRIPT, CACHED_SHARE)

# The legends of the reference lines. Only the expected ones start with EXPECTED.
EXPECTED = "expected"
EXPECTED_LOW = f"{EXPECTED} low"
EXPECTED_HIGH = f"{EXPECTED} high"
FLOOR_MEDIAN = "floor: median stream"
FLOOR_SLOWEST_TENTH = "floor: slowest tenth"
FLOOR_FIRST_TOKEN = "floor: 95th percentile"

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
        )
    ),
    *(
        rules.EmittedBy(name, GATEWAY)
        for name in (GATEWAY_SPEED, GATEWAY_INPUT, GATEWAY_OUTPUT, GATEWAY_FIRST_TOKEN, GATEWAY_FIRST_ANSWER)
    ),
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


def _line(legend: str, value: float) -> rules.Series:
    """A reference line: the same value at every moment, written in full, never rounded."""
    number = float(value)
    written = str(int(number)) if number.is_integer() else repr(number)
    return rules.Series(legend, f"vector({written})")


def _expected(bounds: tuple[float, float] | None) -> list[rules.Series]:
    """The lines of an expected value's low and high bound, or none when the pool gives none."""
    if bounds is None:
        return []
    low, high = bounds
    return [_line(EXPECTED_LOW, low), _line(EXPECTED_HIGH, high)]


def _ratio(pool: Pool, numerator: rules.EngineQuantity, denominator: rules.EngineQuantity, sample: str = "") -> str:
    """One sum of rates over another, both for the pool: a mean that needs no buckets."""
    return f"{rules.engine_rate(pool, numerator, sample)} / {rules.engine_rate(pool, denominator, sample)}"


def _gateway(pool: Pool) -> dict[str, str]:
    return {rules.POOL: pool.name}


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


def panels(pool: Pool, config: Config) -> list[timeseries_builders.Panel]:
    """The row of the pool: each panel that has a measured series, in the order of `TITLES`.

    Reference lines are drawn beside the measured series, and do not keep a panel on their own.
    """
    speed_series, speed_lines = speed(pool, config)
    first_series, first_lines = first_token(pool, config)
    rows: list[tuple[str, list[rules.Series], list[rules.Series], str]] = [
        (SPEED, speed_series, speed_lines, "short"),
        (THROUGHPUT, throughput(pool), [], "short"),
        (FIRST_TOKEN, first_series, first_lines, "s"),
        (INPUT, input_throughput(pool), [], "short"),
        (PREFILL, prefill(pool), [], "short"),
        (TRANSCRIPT, transcript(pool), [], "short"),
        (CACHED_SHARE, cached_share(pool), [], "percentunit"),
    ]
    found = []
    for title, measured, references, unit in rows:
        if not measured:
            continue
        panel = rules.pool_timeseries(pool, title, [*measured, *references]).unit(unit).min(0)
        if title == CACHED_SHARE:
            panel = _flag(panel.max(1), config.cached_share_flag_percent / 100)
        found.append(panel)
    return found


def dashboard(config: Config) -> dashboard_builders.Dashboard:
    """The dashboard, with one row for each pool of the configuration, in its order."""
    built = dashboard_builders.Dashboard(TITLE).uid(UID)
    for pool in config.pools:
        built.with_row(dashboard_builders.Row(pool.name))
        for panel in panels(pool, config):
            built.with_panel(panel.span(HALF))
    return built
