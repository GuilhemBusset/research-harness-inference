"""The rules every panel follows, as the helpers a dashboard module builds its panels with.

Section 7.2 of the system design gives the dashboard code six rules, because each has a way of
showing a wrong number. Three belong to every panel, and are built here:

- **Rates are taken per series and then summed.** An instance's counters restart from zero every
  time it starts, and a rate over a sum would read that as a fall. `rate` and `increase` write
  `sum by (pool) (rate(<series>[<window>]))`: the function's argument is always a plain selector.
- **A pool at zero has no engine series at all.** Its panels take the pool's state from the
  scaler. `engine_timeseries` and `engine_stat` do two things with the scaler's in-service count.
  They keep every engine query only while the count is above zero (`awake`), so that what an
  instance last reported is never shown for a pool that has none. And they add one series that
  exists only while the count is zero (`asleep`), whose value Grafana displays as the text
  "asleep". A stat panel asks for the value now, not the last one of the dashboard's range: it
  reads the engine's numbers while the pool is in service and "asleep" when it is not.
- **A percentile is shown with the number of requests behind it.** `percentile_timeseries` and
  `engine_percentile_timeseries` add, beside the quantiles, the increase of the histogram's count
  over the same window.

**An objective is read over the minutes a session was on** (section 7.3). PromQL knows nothing of
the session switch, so the scaler sends a gauge, `rhi_scaler_session_on`, on every run: 1 while the
switch reads on. `over_sessions` evaluates an expression at every minute of the dashboard's range,
a subquery of one-minute steps, keeps it at a step only where every reading of the gauge in the
minute up to the step is 1, or, where the gauge has no reading in that minute, its last one was
(`in_session`), and reduces the steps kept to one value: their sum, or another `*_over_time`. So a
minute in which the switch read off at any run is not counted, whatever it read at the minute's
ends, and one in which every run read on is, from its start: the gauge says no more of the minute
than its readings.

What is gated is each minute's own contribution. A counter's is `minute_increase`: what it rose by
from each of its samples to the next in the minute, for each series, then summed. It is read at
steps of `SAMPLE_STEP`, shorter than any scrape interval (every collector scrapes each 15 seconds),
so that each such step holds at most one new sample: its value less the value before it, or all
its value where it fell (a restart from zero) or was not there before (the first request of a new
label). A restart between two steps of a minute is so counted as a restart, where the minute's two
ends alone would read through it. The steps share their ends, so every increment falls in exactly
one minute, where adjacent `increase` windows would drop the one between their last and first
scrapes. Counts are summed over the session's minutes before a ratio is taken of them. An engine
series read so is kept to the pool in service at the same step (`awake`, directly inside the gate):
a value over a past range has no "now" to read "asleep" at, and reads no data when no minute had an
instance.

An engine names its metrics its own way, so an engine query never writes a name: `engine_series`
reads it from the per-engine table (`rhi_common.engine_metrics`), at the row of the engine the
pool's configuration names. That holds for the contract's quantities (`Quantity`) and for the
further ones a row may leave out (`Further`): `engine_reports` says whether the pool's row names
one, and a panel draws no series of a quantity its pool's engine does not give. A further name that
is only part of its family carries the labels that select it (vLLM's computed input is
`source="local_compute"`), and every selector the helpers write for it holds them. Every query a helper builds names its data source by the fixed
identifier the Grafana image provisions (`services/grafana/provisioning/datasources/`).

A dashboard module also declares where each metric name it queries comes from, in its `METRICS`:
`FromEngineTable`, `EmittedBy` a file of this repository, or `ExportedBy` an outside exporter at
its pinned version. `rhi_dashboards.check` holds a built dashboard to all of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from grafana_foundation_sdk.builders import common as common_builders
from grafana_foundation_sdk.builders import prometheus as prometheus_builders
from grafana_foundation_sdk.builders import stat as stat_builders
from grafana_foundation_sdk.builders import timeseries as timeseries_builders
from grafana_foundation_sdk.models import common
from grafana_foundation_sdk.models import dashboard as models

from rhi_common.engine_metrics import Further, Kind, Quantity, further_names, metric_names

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from rhi_common.pool_config import Pool

# The two data sources, by the identifiers the image provisions them under.
PROMETHEUS_UID = "rhi-prometheus"
CLOUDWATCH_UID = "rhi-cloudwatch"
PROMETHEUS = common.DataSourceRef(type_val="prometheus", uid=PROMETHEUS_UID)
CLOUDWATCH = common.DataSourceRef(type_val="cloudwatch", uid=CLOUDWATCH_UID)

# The label every series carries for its pool: the collector adds it to the engine's (7.2).
POOL = "pool"
# The scaler's count of a pool's instances in service: the pool's state.
IN_SERVICE = "rhi_scaler_instances_in_service"
# What a panel reads when that count is zero, and the query that carries it.
ASLEEP = "asleep"
ASLEEP_REF = "asleep"
# Grafana's own window for a rate: never shorter than four scrapes.
WINDOW = "$__rate_interval"
# The samples of a counter and of a histogram, after the family's name.
COUNTER_SAMPLE = "_total"
HISTOGRAM_SAMPLES = ("_bucket", "_sum", "_count")


# A quantity of the per-engine table: one of the contract's, or one of the further ones.
type EngineQuantity = Quantity | Further


@dataclass(frozen=True)
class FromEngineTable:
    """A quantity of the per-engine table: each engine's row that names it gives its name."""

    quantity: EngineQuantity


@dataclass(frozen=True)
class EmittedBy:
    """A metric family a file of this repository emits. `path` is relative to the repository."""

    name: str
    path: str


@dataclass(frozen=True)
class ExportedBy:
    """A metric family an outside exporter emits, at the one version the system pins it to."""

    name: str
    exporter: str
    version: str


type Declared = FromEngineTable | EmittedBy | ExportedBy

# The scaler's gauge of the session switch: 1 while it reads on, 0 otherwise (section 7.3).
SESSION_ON = "rhi_scaler_session_on"
# The subquery an objective is read over: every minute of the dashboard's range. Each step's
# contribution is what happened since the step before, so the steps tile the range.
SESSION_STEP = "1m"
SESSION_RANGE = "$__range"
# The steps a counter's minute is read in: shorter than any scrape interval (15 seconds), so that
# each holds at most one new sample of a series.
SAMPLE_STEP = "5s"

# What the helpers themselves query. `rhi_dashboards.check` adds it to every module's own.
METRICS: tuple[Declared, ...] = (
    EmittedBy(IN_SERVICE, "services/scaler/rhi_scaler/metrics.py"),
    EmittedBy(SESSION_ON, "services/scaler/rhi_scaler/metrics.py"),
)


def selector(metric: str, matchers: Mapping[str, str] | None = None) -> str:
    """`metric{label="value"}`: the series of a metric whose labels equal the values given."""
    if not matchers:
        return metric
    quoted = ", ".join(
        '{}="{}"'.format(label, value.replace("\\", "\\\\").replace('"', '\\"')) for label, value in matchers.items()
    )
    return f"{metric}{{{quoted}}}"


def _summed(function: str, metric: str, matchers: Mapping[str, str] | None, window: str, by: Sequence[str]) -> str:
    labels = ", ".join((POOL, *by))
    return f"sum by ({labels}) ({function}({selector(metric, matchers)}[{window}]))"


def rate(
    metric: str, matchers: Mapping[str, str] | None = None, *, window: str = WINDOW, by: Sequence[str] = ()
) -> str:
    """The rate of a counter, a second: taken for each series, then summed by pool and by `by`."""
    return _summed("rate", metric, matchers, window, by)


def increase(
    metric: str, matchers: Mapping[str, str] | None = None, *, window: str = WINDOW, by: Sequence[str] = ()
) -> str:
    """The increase of a counter over the window: taken for each series, then summed by pool and by `by`."""
    return _summed("increase", metric, matchers, window, by)


def quantile(
    q: float,
    histogram: str,
    matchers: Mapping[str, str] | None = None,
    *,
    window: str = WINDOW,
    by: Sequence[str] = (),
) -> str:
    """The quantile `q`, from 0 to 1, of a histogram family, by pool and by `by`."""
    return f"histogram_quantile({q:g}, {rate(histogram + '_bucket', matchers, window=window, by=(*by, 'le'))})"


def request_count(
    histogram: str, matchers: Mapping[str, str] | None = None, *, window: str = WINDOW, by: Sequence[str] = ()
) -> str:
    """How many observations a histogram family took over the window: the requests behind a quantile."""
    return increase(histogram + "_count", matchers, window=window, by=by)


def _engine_name(pool: Pool, quantity: EngineQuantity) -> tuple[str, Kind, Mapping[str, str]]:
    """The family's name in the row of the pool's engine, its kind, and the labels that select the quantity."""
    if isinstance(quantity, Quantity):
        family = metric_names(pool.engine)[quantity]
        return family, quantity.kind, getattr(family, "labels", None) or {}
    named = further_names(pool.engine)
    if quantity not in named:
        message = f"the row of the engine {pool.engine!r} leaves out {quantity.value}"
        raise KeyError(message)
    return named[quantity], named[quantity].kind, named[quantity].labels


def engine_reports(pool: Pool, quantity: EngineQuantity) -> bool:
    """Whether the row of the pool's engine names the quantity: every row names each `Quantity`,
    and may leave out any `Further` one."""
    return isinstance(quantity, Quantity) or quantity in further_names(pool.engine)


def engine_series(pool: Pool, quantity: EngineQuantity, sample: str = "") -> str:
    """The series of a quantity, as the pool's engine names it, for this pool.

    The name is the per-engine table's, at the row of `pool.engine`. A counter's series carries
    `_total`; a histogram's is one of `_bucket`, `_sum` and `_count`, which `sample` names. An
    engine with no row, or a further quantity its row leaves out, raises `KeyError`.
    """
    family, kind, _ = _engine_name(pool, quantity)
    if kind is Kind.COUNTER:
        sample = COUNTER_SAMPLE
    elif kind is Kind.HISTOGRAM and sample not in HISTOGRAM_SAMPLES:
        message = f"{quantity.value} is a histogram: name one of its samples, {', '.join(HISTOGRAM_SAMPLES)}"
        raise ValueError(message)
    return family + sample


def engine_matchers(pool: Pool, quantity: EngineQuantity) -> dict[str, str]:
    """The matchers of the quantity's series for this pool: its pool, and the labels that select it."""
    return {POOL: pool.name, **_engine_name(pool, quantity)[2]}


def engine_selector(pool: Pool, quantity: EngineQuantity, sample: str = "") -> str:
    """The selector of `engine_series`, for a gauge or for an expression written by hand."""
    return selector(engine_series(pool, quantity, sample), engine_matchers(pool, quantity))


def engine_rate(
    pool: Pool, quantity: EngineQuantity, sample: str = "", *, window: str = WINDOW, by: Sequence[str] = ()
) -> str:
    """`rate` of an engine's counter, or of a histogram's `_sum` or `_count`, for this pool."""
    return rate(engine_series(pool, quantity, sample), engine_matchers(pool, quantity), window=window, by=by)


def engine_increase(
    pool: Pool, quantity: EngineQuantity, sample: str = "", *, window: str = WINDOW, by: Sequence[str] = ()
) -> str:
    """`increase` of an engine's counter, or of a histogram's `_sum` or `_count`, for this pool."""
    return increase(engine_series(pool, quantity, sample), engine_matchers(pool, quantity), window=window, by=by)


def _engine_histogram(pool: Pool, quantity: EngineQuantity) -> str:
    family, kind, _ = _engine_name(pool, quantity)
    if kind is not Kind.HISTOGRAM:
        message = f"{quantity.value} is not a histogram"
        raise ValueError(message)
    return family


def engine_quantile(
    pool: Pool, q: float, quantity: EngineQuantity, *, window: str = WINDOW, by: Sequence[str] = ()
) -> str:
    """`quantile` of a histogram of the pool's engine, for this pool."""
    histogram = _engine_histogram(pool, quantity)
    return quantile(q, histogram, engine_matchers(pool, quantity), window=window, by=by)


def engine_request_count(pool: Pool, quantity: EngineQuantity, *, window: str = WINDOW, by: Sequence[str] = ()) -> str:
    """`request_count` of a histogram of the pool's engine, for this pool: the requests behind its quantiles."""
    histogram = _engine_histogram(pool, quantity)
    return request_count(histogram, engine_matchers(pool, quantity), window=window, by=by)


def in_service(pool: str) -> str:
    """The scaler's count of the pool's instances in service."""
    return selector(IN_SERVICE, {POOL: pool})


def awake(pool: str, expression: str) -> str:
    """The expression's series while the scaler counts an instance in service, and none otherwise.

    With no instance, nothing reports: what an expression still returns then is what an instance
    last said, which Prometheus keeps for some minutes.
    """
    return f"({expression}) and on ({POOL}) ({in_service(pool)} > 0)"


def asleep(pool: str) -> str:
    """One series, of value zero, while the scaler counts no instance in service; none otherwise."""
    return f"{in_service(pool)} == 0"


def in_session(expression: str) -> str:
    """The expression's series at a step where every reading of the scaler's session gauge in the
    minute up to it is 1, or, with no reading in that minute, the last one before it is; none
    elsewhere. A minute in which the gauge read anything else, even once, is not kept."""
    every = f"min_over_time({SESSION_ON}[{SESSION_STEP}]) == 1"
    none_in_it = f"({SESSION_ON} == 1) unless on () (count_over_time({SESSION_ON}[{SESSION_STEP}]))"
    return f"({expression}) and on () (({every}) or on () ({none_in_it}))"


def over_sessions(expression: str, function: str = "sum_over_time") -> str:
    """The expression kept to the session, at every minute of the dashboard's range, reduced by
    `function` (a `*_over_time`) to one value for each of its series. Give it each minute's
    contribution: `minute_increase`, or a count of what happened in that minute."""
    return f"{function}(({in_session(expression)})[{SESSION_RANGE}:{SESSION_STEP}])"


def minute_increase(metric: str, matchers: Mapping[str, str] | None = None, *, by: Sequence[str] = ()) -> str:
    """What a counter rose by over one step of `over_sessions`, for each series, then summed by `by`
    alone (no pool unless named): the sum, over the steps of `SAMPLE_STEP` in the minute, of its
    value less its value a step before, or all its value where it was not there a step before or
    where it fell (it restarted from zero). Each step is one series of the same labels whichever
    it takes, nameless (`sum without ()`), so that the minute's sum of it is one series too."""
    series = selector(metric, matchers)
    labels = ", ".join(by)
    step = f"sum without () ((({series} - {series} offset {SAMPLE_STEP}) >= 0) or {series})"
    return f"sum by ({labels}) (sum_over_time(({step})[{SESSION_STEP}:{SAMPLE_STEP}]))"


def engine_minute_increase(pool: Pool, quantity: EngineQuantity, sample: str = "") -> str:
    """`minute_increase` of an engine's counter, for this pool, summed by pool."""
    return minute_increase(engine_series(pool, quantity, sample), engine_matchers(pool, quantity), by=(POOL,))


def query(expression: str, legend: str, ref_id: str, *, instant: bool = False) -> prometheus_builders.Dataquery:
    """A query of the Prometheus data source, over the dashboard's range or, `instant`, for its end."""
    built = prometheus_builders.Dataquery().expr(expression).legend_format(legend).ref_id(ref_id).datasource(PROMETHEUS)
    return built.instant() if instant else built.range()


def _ref(index: int) -> str:
    """The reference of a panel's query: A, B, C and so on."""
    if index >= 26:
        raise ValueError("a panel holds at most 26 queries")
    return chr(ord("A") + index)


def _reads_asleep() -> list[models.DynamicConfigValue]:
    """The value zero displayed as the text "asleep"."""
    mapping = models.ValueMap(options={"0": models.ValueMappingResult(text=ASLEEP, index=0)})
    return [models.DynamicConfigValue(id_val="mappings", value=[mapping])]


def _graph() -> timeseries_builders.Panel:
    """A graph whose legend shows: a series is read by its name, "asleep" among them."""
    return timeseries_builders.Panel().legend(common_builders.VizLegendOptions().show_legend(True))


def _engine[P: (timeseries_builders.Panel, stat_builders.Panel)](
    panel: P, pool: Pool, title: str, queries: Mapping[str, str], *, instant: bool
) -> P:
    panel.title(title).datasource(PROMETHEUS)
    for index, (legend, expression) in enumerate(queries.items()):
        panel.with_target(query(awake(pool.name, expression), legend, _ref(index), instant=instant))
    panel.with_target(query(asleep(pool.name), ASLEEP, ASLEEP_REF, instant=instant))
    # Only that query's zero reads "asleep": an engine's own zero stays a number.
    panel.override_by_query(ASLEEP_REF, _reads_asleep())
    return panel


def engine_timeseries(pool: Pool, title: str, queries: Mapping[str, str]) -> timeseries_builders.Panel:
    """A graph of engine queries for one pool: `queries` maps each legend to its expression.

    Each expression is kept only while the pool has an instance in service. While it has none the
    panel draws one series, "asleep", in their place.
    """
    return _engine(_graph(), pool, title, queries, instant=False)


def engine_stat(pool: Pool, title: str, queries: Mapping[str, str]) -> stat_builders.Panel:
    """The engine's values now, for one pool: `queries` maps each legend to its expression.

    Every query is an instant one, so the panel never reduces the dashboard's range to the last
    value an instance reported. With no instance in service it reads "asleep".
    """
    panel = stat_builders.Panel().reduce_options(common_builders.ReduceDataOptions().calcs(["lastNotNull"]))
    return _engine(panel, pool, title, queries, instant=True)


def _percent(q: float) -> str:
    return f"p{q * 100:g}"


def _count_on_its_own_axis() -> list[models.DynamicConfigValue]:
    """A count is not in the quantiles' unit: it is read on the right-hand axis."""
    return [
        models.DynamicConfigValue(id_val="custom.axisPlacement", value="right"),
        models.DynamicConfigValue(id_val="unit", value="short"),
    ]


def percentile_timeseries(
    title: str,
    histogram: str,
    matchers: Mapping[str, str] | None = None,
    *,
    quantiles: Sequence[float] = (0.5, 0.95),
    window: str = WINDOW,
    by: Sequence[str] = (),
) -> timeseries_builders.Panel:
    """A graph of a histogram family's quantiles, with the count of requests over the same window."""
    panel = _graph().title(title).datasource(PROMETHEUS)
    for index, q in enumerate(quantiles):
        expression = quantile(q, histogram, matchers, window=window, by=by)
        panel.with_target(query(expression, _percent(q), _ref(index)))
    count = _ref(len(quantiles))
    panel.with_target(query(request_count(histogram, matchers, window=window, by=by), "requests", count))
    return panel.override_by_query(count, _count_on_its_own_axis())


def engine_percentile_timeseries(
    pool: Pool,
    title: str,
    quantity: Quantity,
    *,
    quantiles: Sequence[float] = (0.5, 0.95),
    window: str = WINDOW,
) -> timeseries_builders.Panel:
    """`percentile_timeseries` for a histogram of the pool's engine: it reads "asleep" as `engine_timeseries` does."""
    histogram = metric_names(pool.engine)[quantity]
    if quantity.kind is not Kind.HISTOGRAM:
        message = f"{quantity.value} is not a histogram"
        raise ValueError(message)
    matchers = {POOL: pool.name}
    queries = {_percent(q): quantile(q, histogram, matchers, window=window) for q in quantiles}
    queries["requests"] = request_count(histogram, matchers, window=window)
    panel = engine_timeseries(pool, title, queries)
    return panel.override_by_query(_ref(len(quantiles)), _count_on_its_own_axis())


@dataclass(frozen=True)
class Series:
    """One query of a pool's graph, with the name its series is read by.

    `engine` is whether it reads the pool's engine, and so is kept to the pool in service. `axis`
    is the unit of a series read on the right-hand axis, because it is not in the panel's unit (a
    count of requests is "short"); None keeps it on the panel's axis.
    """

    legend: str
    expression: str
    engine: bool = False
    axis: str | None = None


def percentile_series(
    histogram: str,
    matchers: Mapping[str, str] | None = None,
    *,
    quantiles: Sequence[float] = (0.5, 0.95),
    window: str = WINDOW,
    by: Sequence[str] = (),
    legend: str = "",
) -> list[Series]:
    """A histogram family's quantiles and the count of requests behind them, over the same window
    and by the same labels, as series of a pool's graph. `legend` comes before each one's name."""
    found = [
        Series(f"{legend}{_percent(q)}", quantile(q, histogram, matchers, window=window, by=by)) for q in quantiles
    ]
    count = request_count(histogram, matchers, window=window, by=by)
    return [*found, Series(f"{legend}requests", count, axis="short")]


def engine_percentile_series(
    pool: Pool,
    quantity: EngineQuantity,
    *,
    quantiles: Sequence[float] = (0.5, 0.95),
    window: str = WINDOW,
    by: Sequence[str] = (),
    legend: str = "",
) -> list[Series]:
    """`percentile_series` for a histogram of the pool's engine: each kept to the pool in service."""
    histogram = _engine_histogram(pool, quantity)
    found = percentile_series(
        histogram, engine_matchers(pool, quantity), quantiles=quantiles, window=window, by=by, legend=legend
    )
    return [Series(one.legend, one.expression, engine=True, axis=one.axis) for one in found]


def pool_timeseries(pool: Pool, title: str, series: Sequence[Series]) -> timeseries_builders.Panel:
    """A graph of one pool that may mix the engine's series with others: the gateway's, a reference line.

    Each engine series is kept only while the pool has an instance in service, and while it has
    none the panel draws "asleep" in their place, as `engine_timeseries` does. The other series are
    drawn as they are. A panel with no engine series has no "asleep".
    """
    panel = _graph().title(title).datasource(PROMETHEUS)
    for index, one in enumerate(series):
        expression = awake(pool.name, one.expression) if one.engine else one.expression
        panel.with_target(query(expression, one.legend, _ref(index)))
        if one.axis is not None:
            panel.override_by_query(
                _ref(index),
                [
                    models.DynamicConfigValue(id_val="custom.axisPlacement", value="right"),
                    models.DynamicConfigValue(id_val="unit", value=one.axis),
                ],
            )
    if any(one.engine for one in series):
        panel.with_target(query(asleep(pool.name), ASLEEP, ASLEEP_REF))
        panel.override_by_query(ASLEEP_REF, _reads_asleep())
    return panel
