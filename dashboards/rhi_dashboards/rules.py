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

An engine names its metrics its own way, so an engine query never writes a name: `engine_series`
reads it from the per-engine table (`rhi_common.engine_metrics`), at the row of the engine the
pool's configuration names. Every query a helper builds names its data source by the fixed
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

from rhi_common.engine_metrics import Kind, Quantity, metric_names

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


@dataclass(frozen=True)
class FromEngineTable:
    """A quantity of the per-engine table: each engine's row gives its name."""

    quantity: Quantity


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

# What the helpers themselves query. `rhi_dashboards.check` adds it to every module's own.
METRICS: tuple[Declared, ...] = (EmittedBy(IN_SERVICE, "services/scaler/rhi_scaler/metrics.py"),)


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


def engine_series(pool: Pool, quantity: Quantity, sample: str = "") -> str:
    """The series of a quantity, as the pool's engine names it, for this pool.

    The name is the per-engine table's, at the row of `pool.engine`. A counter's series carries
    `_total`; a histogram's is one of `_bucket`, `_sum` and `_count`, which `sample` names. An
    engine with no row raises `KeyError`.
    """
    family = metric_names(pool.engine)[quantity]
    if quantity.kind is Kind.COUNTER:
        sample = COUNTER_SAMPLE
    elif quantity.kind is Kind.HISTOGRAM and sample not in HISTOGRAM_SAMPLES:
        message = f"{quantity.value} is a histogram: name one of its samples, {', '.join(HISTOGRAM_SAMPLES)}"
        raise ValueError(message)
    return family + sample


def engine_selector(pool: Pool, quantity: Quantity, sample: str = "") -> str:
    """The selector of `engine_series`, for a gauge or for an expression written by hand."""
    return selector(engine_series(pool, quantity, sample), {POOL: pool.name})


def engine_rate(
    pool: Pool, quantity: Quantity, sample: str = "", *, window: str = WINDOW, by: Sequence[str] = ()
) -> str:
    """`rate` of an engine's counter, or of a histogram's `_sum` or `_count`, for this pool."""
    return rate(engine_series(pool, quantity, sample), {POOL: pool.name}, window=window, by=by)


def engine_increase(
    pool: Pool, quantity: Quantity, sample: str = "", *, window: str = WINDOW, by: Sequence[str] = ()
) -> str:
    """`increase` of an engine's counter, or of a histogram's `_sum` or `_count`, for this pool."""
    return increase(engine_series(pool, quantity, sample), {POOL: pool.name}, window=window, by=by)


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
