"""The rules every panel follows, as the helpers a dashboard module builds its panels with.

Section 7.2 of the system design gives the dashboard code six rules, because each has a way of
showing a wrong number. Three belong to every panel, and are built here:

- **Rates are taken per series and then summed.** An instance's counters restart from zero every
  time it starts, and a rate over a sum would read that as a fall. `rate` and `increase` write
  `sum by (pool) (rate(<series>[<window>]))`: the function's argument is always a plain selector.
- **A pool with no instance has no engine series at all.** Its panels take the pool's state from
  the scaler, which is one of two: "no session" while the pool's session is not active, and
  "waiting for capacity" while it is active and no instance is in service. `engine_timeseries`,
  `engine_stat`, `pool_timeseries` and `engine_table` do two things with the scaler's two gauges
  of the pool, its session and its count of instances in service. They keep every engine query
  only while the session is active and the count is above zero (`serving`), so that what an
  instance last reported is never shown for a pool that has none, and a panel that reads "no
  session" shows nothing else of the engine. And they add one query for each state
  (`no_session`, `waiting_for_capacity`), which has a series only while the pool is in that
  state, whose value Grafana displays as the state's text. A stat panel and a table ask for the
  value now, not the last one of the dashboard's range.
- **A percentile is shown with the number of requests behind it.** `percentile_timeseries` and
  `engine_percentile_timeseries` add, beside the quantiles, the increase of the histogram's count
  over the same window.

**An objective is read over the time a session was active** (section 7.3). Sessions are per pool,
and PromQL knows nothing of them, so the scaler sends a gauge for each pool,
`rhi_scaler_session_active`, on every run, once a minute, stamped with the run's time: 1 while the
pool's session is active. An objective of a pool is read in that pool's session, from the gauge
with the pool's label. The gateway's objective is about no pool: it is read while any pool's
session is active (`ANY_POOL`), from the gauges of every pool, which one run stamps with one time.
What is counted is each sample of a counter, by what it adds: `session_increment` takes, for each
series, its value less its value at the step before, or all of it where it fell (a restart from
zero) or was not there before (the first request of a new label), and keeps it only where that
sample was taken while the session was active, inside the dashboard's range:

- **By the reading in force when it was taken.** A sample is compared with the gauge's readings by
  their timestamps (`timestamp`), not by the step it falls in: it is counted when the latest reading
  at or before its own time was 1. That reading is the gauge's latest at the step, if it was taken
  no later than the sample; otherwise it is the latest at the step before, the gauge being read at
  most once a step. So a scrape taken while the session read not active is not counted, even a
  second before it began, and one taken while it read active is, even a second before it ended.
- **Inside the range.** A sample taken at or before the range's start (`$__from`) is not counted.

`over_sessions` sums those increments at every step of `SESSION_STEP` across the dashboard's range,
a subquery over exactly the range (`$__range_ms`), and adds what came after its last step, which
lies on a multiple of the step at or before the range's end: the increment of each series since
that step, read at the end. So every sample taken in the range is counted once and none from
outside it. The step, 5 seconds, is shorter than any scrape interval (every collector scrapes each
15 seconds), so each step holds at most one new sample of a series, and each sample is judged by
its own time. Counts are summed over the session before a ratio is taken of them. A series no
instance updates any more rises by nothing, so an engine series read so counts nothing an instance
last reported, and needs no state of the pool: over a past range there is no "now" to read it at.

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
from grafana_foundation_sdk.builders import table as table_builders
from grafana_foundation_sdk.builders import timeseries as timeseries_builders
from grafana_foundation_sdk.models import common, prometheus
from grafana_foundation_sdk.models import dashboard as models

from rhi_common.engine_metrics import Further, Kind, Quantity, further_names, metric_names

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from rhi_common.pool_config import Pool

# The two data sources, by the identifiers the image provisions them under.
PROMETHEUS_UID = "rhi-prometheus"
CLOUDWATCH_UID = "rhi-cloudwatch"
PROMETHEUS = common.DataSourceRef(type_val="prometheus", uid=PROMETHEUS_UID)
CLOUDWATCH = common.DataSourceRef(type_val="cloudwatch", uid=CLOUDWATCH_UID)
# Grafana's own, which are no data source: its server-side expressions, which compute from the
# queries of a panel, and the mixed source of a panel whose queries each name their own.
EXPRESSIONS = common.DataSourceRef(type_val="__expr__", uid="__expr__")
MIXED = common.DataSourceRef(type_val="datasource", uid="-- Mixed --")

# The label every series carries for its pool: the collector adds it to the engine's (7.2).
POOL = "pool"
# The scaler's two gauges of a pool's state (section 7.2): whether its session is active, 1 or 0,
# and its count of instances in service.
SESSION_ACTIVE = "rhi_scaler_session_active"
IN_SERVICE = "rhi_scaler_instances_in_service"
# The label the collector gives every series of an instance: the instance's ID.
INSTANCE = "instance"
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


@dataclass(frozen=True)
class State:
    """A state a pool's panel reads in place of its engine's series: the text it reads, and the
    reference of the query that carries it."""

    text: str
    ref: str


# The two states, from the scaler (section 7.2).
NO_SESSION = State("no session", "no_session")
WAITING_FOR_CAPACITY = State("waiting for capacity", "waiting_for_capacity")
STATES = (NO_SESSION, WAITING_FOR_CAPACITY)

# Whose session an objective is read in: a pool's, by the pool's name, or any pool's.
type Session = str | None
ANY_POOL: Session = None
# The steps an objective is counted at: each shorter than any scrape interval (15 seconds), so that
# each holds at most one new sample of a series, across exactly the dashboard's range, which starts
# at `SESSION_START`, in seconds.
SESSION_STEP = "5s"
SESSION_RANGE = "${__range_ms}ms"
SESSION_START = "$__from / 1000"

# What the helpers themselves query. `rhi_dashboards.check` adds it to every module's own.
METRICS: tuple[Declared, ...] = (
    EmittedBy(IN_SERVICE, "services/scaler/rhi_scaler/metrics.py"),
    EmittedBy(SESSION_ACTIVE, "services/scaler/rhi_scaler/metrics.py"),
)


def selector(metric: str, matchers: Mapping[str, str] | None = None, *, matching: Mapping[str, str] | None = None) -> str:
    """`metric{label="value", other=~"pattern"}`: the series of a metric whose labels equal the
    values of `matchers` and match the regular expressions of `matching`."""
    pairs = [(label, "=", value) for label, value in (matchers or {}).items()]
    pairs += [(label, "=~", value) for label, value in (matching or {}).items()]
    if not pairs:
        return metric
    quoted = ", ".join(
        '{}{}"{}"'.format(label, op, value.replace("\\", "\\\\").replace('"', '\\"')) for label, op, value in pairs
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


def session_active(pool: str) -> str:
    """The scaler's gauge of the pool's session: 1 while it is active, 0 otherwise."""
    return selector(SESSION_ACTIVE, {POOL: pool})


def serving(pool: str, expression: str) -> str:
    """The expression's series while the pool's session is active and the scaler counts an
    instance in service, and none otherwise.

    With no instance, nothing reports: what an expression still returns then is what an instance
    last said, which Prometheus keeps for some minutes. And outside the session the panel reads
    "no session", whatever is still in service: an instance that drains after its session's end.
    """
    return (
        f"({expression}) and on ({POOL}) ({in_service(pool)} > 0) and on ({POOL}) ({session_active(pool)} == 1)"
    )


def no_session(pool: str) -> str:
    """One series, of value zero, while the pool's session is not active; none otherwise."""
    return f"{session_active(pool)} == 0"


def waiting_for_capacity(pool: str) -> str:
    """One series, of value zero, while the pool's session is active and the scaler counts no
    instance in service; none otherwise."""
    return f"({in_service(pool)} == 0) and on ({POOL}) ({session_active(pool)} == 1)"


def state_queries(pool: str) -> dict[State, str]:
    """The query of each state of the pool. At most one has a series at any moment, and none has
    while the pool serves (`serving`)."""
    return {NO_SESSION: no_session(pool), WAITING_FOR_CAPACITY: waiting_for_capacity(pool)}


# What an expression read at the step before the one it is evaluated at.
type Previous = Callable[[str], str]
# What one step adds, given how to read the step before it: see `over_sessions`.
type Contribution = Callable[[Previous], str]


def a_step_before(expression: str) -> str:
    """The expression at the step before, inside the subquery of `over_sessions`."""
    return f"{expression} offset {SESSION_STEP}"


def at_the_last_step(expression: str) -> str:
    """The expression at the last step of the subquery of `over_sessions`, read at the range's end:
    the step on the multiple of `SESSION_STEP` at or before it."""
    return f"last_over_time({expression}[{SESSION_STEP}:{SESSION_STEP}])"


# The two ways a step before is read, in that order.
PREVIOUS: tuple[Previous, ...] = (a_step_before, at_the_last_step)


def taken_in_session(series: str, previous: Previous, session: Session) -> str:
    """The series' samples taken after the range's start while the latest reading of the session
    was 1: the reading at this step if taken no later than the sample, else the one at `previous`.

    `session` is a pool's name, and the reading is that pool's gauge; or `ANY_POOL`, and it is the
    gauges of every pool, 1 where any pool's is. Either way it is read as one series: the time of
    the latest reading, and the largest value read."""
    gauge = selector(SESSION_ACTIVE, None if session is None else {POOL: session})
    taken = f"max by () (timestamp({gauge}))"
    since = f"timestamp({series}) > {SESSION_START}"
    after = f"timestamp({series}) >= on () group_left () ({taken} and on () (max by () ({gauge}) == 1))"
    before = (
        f"timestamp({series}) < on () group_left () ({taken} and on () (max by () ({previous(gauge)}) == 1))"
    )
    return f"(({since}) and (({after}) or ({before})))"


def session_increment(series: str, previous: Previous, session: Session) -> str:
    """What each series of the selector rose by since `previous`, kept where its sample was taken
    while the session was active, inside the range: its value less its value then, or all of it
    where it was not there then or fell (it restarted from zero)."""
    risen = f"(({series} - {previous(series)}) >= 0) or {series}"
    return f"(({risen}) and {taken_in_session(series, previous, session)})"


def over_sessions(contribution: Contribution) -> str:
    """What the steps of the dashboard's range contributed while a session was active, summed: at
    every step of `SESSION_STEP` across it, and from its last step to its end. `contribution` writes
    one step's, given how to read the step before; it aggregates `session_increment`s."""
    steps = f"sum_over_time(({contribution(a_step_before)})[{SESSION_RANGE}:{SESSION_STEP}])"
    last = contribution(at_the_last_step)
    return f"(({steps}) + ({last})) or ({steps}) or ({last})"


def step_increase(
    metric: str, matchers: Mapping[str, str] | None = None, *, session: Session, by: Sequence[str] = ()
) -> Contribution:
    """A step's contribution of a counter: the `session_increment` of each of its series, in the
    session of the pool `session` names or of any pool (`ANY_POOL`), summed by `by` alone (no pool
    unless named)."""
    series = selector(metric, matchers)
    labels = ", ".join(by)
    return lambda previous: f"sum by ({labels}) ({session_increment(series, previous, session)})"


def engine_step_increase(pool: Pool, quantity: EngineQuantity, sample: str = "") -> Contribution:
    """`step_increase` of an engine's counter, for this pool, in this pool's session, summed by pool."""
    series, matchers = engine_series(pool, quantity, sample), engine_matchers(pool, quantity)
    return step_increase(series, matchers, session=pool.name, by=(POOL,))


def query(expression: str, legend: str, ref_id: str, *, instant: bool = False) -> prometheus_builders.Dataquery:
    """A query of the Prometheus data source, over the dashboard's range or, `instant`, for its end."""
    built = prometheus_builders.Dataquery().expr(expression).legend_format(legend).ref_id(ref_id).datasource(PROMETHEUS)
    return built.instant() if instant else built.range()


def _ref(index: int) -> str:
    """The reference of a panel's query: A, B, C and so on."""
    if index >= 26:
        raise ValueError("a panel holds at most 26 queries")
    return chr(ord("A") + index)


def _reads(state: State) -> list[models.DynamicConfigValue]:
    """The value zero displayed as the state's text."""
    mapping = models.ValueMap(options={"0": models.ValueMappingResult(text=state.text, index=0)})
    return [models.DynamicConfigValue(id_val="mappings", value=[mapping])]


def _graph() -> timeseries_builders.Panel:
    """A graph whose legend shows: a series is read by its name, the pool's state among them."""
    return timeseries_builders.Panel().legend(common_builders.VizLegendOptions().show_legend(True))


def _with_state[P: (timeseries_builders.Panel, stat_builders.Panel)](panel: P, pool: Pool, *, instant: bool) -> P:
    """Adds the query of each state of the pool, which reads the state's text."""
    for state, expression in state_queries(pool.name).items():
        panel.with_target(query(expression, state.text, state.ref, instant=instant))
        # Only that query's zero reads the text: an engine's own zero stays a number.
        panel.override_by_query(state.ref, _reads(state))
    return panel


def _engine[P: (timeseries_builders.Panel, stat_builders.Panel)](
    panel: P, pool: Pool, title: str, queries: Mapping[str, str], *, instant: bool
) -> P:
    panel.title(title).datasource(PROMETHEUS)
    for index, (legend, expression) in enumerate(queries.items()):
        panel.with_target(query(serving(pool.name, expression), legend, _ref(index), instant=instant))
    return _with_state(panel, pool, instant=instant)


def engine_timeseries(pool: Pool, title: str, queries: Mapping[str, str]) -> timeseries_builders.Panel:
    """A graph of engine queries for one pool: `queries` maps each legend to its expression.

    Each expression is kept only while the pool serves: its session is active and it has an
    instance in service. Otherwise the panel draws one series in their place, "no session" or
    "waiting for capacity".
    """
    return _engine(_graph(), pool, title, queries, instant=False)


def engine_stat(pool: Pool, title: str, queries: Mapping[str, str]) -> stat_builders.Panel:
    """The engine's values now, for one pool: `queries` maps each legend to its expression.

    Every query is an instant one, so the panel never reduces the dashboard's range to the last
    value an instance reported. While the pool does not serve it reads "no session" or "waiting
    for capacity".
    """
    panel = stat_builders.Panel().reduce_options(common_builders.ReduceDataOptions().calcs(["lastNotNull"]))
    return _engine(panel, pool, title, queries, instant=True)


@dataclass(frozen=True)
class Column:
    """One column of a pool's table of instances: its header, its query, which has one series for
    each instance, and its unit, by Grafana's unit identifiers."""

    header: str
    expression: str
    unit: str


# The header of the column that reads each state of the pool: a table names its columns apart.
STATE_HEADERS = {NO_SESSION: "Session", WAITING_FOR_CAPACITY: "Capacity"}
# The fields of a query's frame that are not shown: the time the values were read at, the pool,
# which is the row's, and the name of the scaler's gauge, which a state's query keeps.
TABLE_HIDDEN = ("Time", POOL, "__name__")


def table_value(ref: str) -> str:
    """The name Grafana gives the value of a query in a table of several queries: by the query's reference."""
    return f"Value #{ref}"


def engine_table(pool: Pool, title: str, columns: Sequence[Column]) -> table_builders.Panel:
    """A table with one line for each instance of the pool, and one column for each of `columns`.

    Every query asks for the value now, as a table, and is kept only while the pool serves
    (`serving`): each has one series an instance, labelled by it, and the lines of one instance
    are merged into one. Otherwise the table has one line, which reads "no session" or "waiting
    for capacity" in the column of that state (`STATE_HEADERS`). A column is named by its header
    through the name Grafana gives its query's value (`table_value`): a query with no series
    still answers with an empty table, so a panel of several queries always names each value by
    its query.
    """
    panel = table_builders.Panel().title(title).datasource(PROMETHEUS)
    for index, column in enumerate(columns):
        ref = _ref(index)
        built = query(serving(pool.name, column.expression), column.header, ref, instant=True)
        panel.with_target(built.format(prometheus.PromQueryFormat.TABLE))
        panel.override_by_name(
            table_value(ref),
            [
                models.DynamicConfigValue(id_val="displayName", value=column.header),
                models.DynamicConfigValue(id_val="unit", value=column.unit),
            ],
        )
    for state, expression in state_queries(pool.name).items():
        built = query(expression, state.text, state.ref, instant=True)
        panel.with_target(built.format(prometheus.PromQueryFormat.TABLE))
        named = models.DynamicConfigValue(id_val="displayName", value=STATE_HEADERS[state])
        panel.override_by_name(table_value(state.ref), [named, *_reads(state)])
    panel.with_transformation(models.DataTransformerConfig(id_val="merge", options={}))
    hidden = dict.fromkeys(TABLE_HIDDEN, True)
    return panel.with_transformation(
        models.DataTransformerConfig(id_val="organize", options={"excludeByName": hidden})
    )


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
    """`percentile_timeseries` for a histogram of the pool's engine: it reads the pool's state as `engine_timeseries` does."""
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

    `engine` is whether it reads the pool's engine, and so is kept to the pool while it serves. `axis`
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
    """`percentile_series` for a histogram of the pool's engine: each kept to the pool while it serves."""
    histogram = _engine_histogram(pool, quantity)
    found = percentile_series(
        histogram, engine_matchers(pool, quantity), quantiles=quantiles, window=window, by=by, legend=legend
    )
    return [Series(one.legend, one.expression, engine=True, axis=one.axis) for one in found]


def pool_timeseries(pool: Pool, title: str, series: Sequence[Series]) -> timeseries_builders.Panel:
    """A graph of one pool that may mix the engine's series with others: the gateway's, a reference line.

    Each engine series is kept only while the pool serves, and otherwise the panel draws the pool's
    state in their place, as `engine_timeseries` does. The other series are drawn as they are. A
    panel with no engine series has no state to read.
    """
    panel = _graph().title(title).datasource(PROMETHEUS)
    for index, one in enumerate(series):
        expression = serving(pool.name, one.expression) if one.engine else one.expression
        panel.with_target(query(expression, one.legend, _ref(index)))
        if one.axis is not None:
            panel.override_by_query(
                _ref(index),
                [
                    models.DynamicConfigValue(id_val="custom.axisPlacement", value="right"),
                    models.DynamicConfigValue(id_val="unit", value=one.axis),
                ],
            )
    return _with_state(panel, pool, instant=False) if any(one.engine for one in series) else panel
