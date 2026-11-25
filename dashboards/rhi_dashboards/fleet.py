"""The fleet and scaling dashboard: what each session requested against what is available, and each instance's telemetry.

The first of the six dashboards of the system design (section 7.3), for requirement R5. Its first
row shows the four objectives of section 7.3 (`rhi_dashboards.objectives`), each against its
target, each pool's over the time its session was active in the dashboard's time range: one stat
per measure, green where it meets its target and red where it does not, with the count of
observations behind a percentile beside it. A
percentile is shown as the share of observations it is judged on (`rhi_dashboards.objectives`), and
a value that must be above its threshold turns green only above it: at the threshold it is red. A
share an older gateway task's buckets only bound reads "inconclusive", in orange, where its bounds
straddle the threshold.
Each is one value for the whole range, so its queries are instant ones, read at the range's end over
exactly the range (`$__from` to its end); a range with no session reads no data.

Then one row for each pool of the configuration, which answers, for that pool, what was requested
and what is available (section 7.3):

| Panel | Shows | From |
| --- | --- | --- |
| Session | Whether the pool's session is active, and the session's start and end | The scaler's gauges (section 7.2) |
| Instances | Over time, the instances requested, asked of AWS, waiting for capacity, starting and in service | The scaler's gauges |
| Waiting for capacity | The seconds the pool has waited, and the cause of the last failed launch | The scaler's gauges |
| Scale decisions | The decisions made, counted by reason | The scaler's counter |
| Engine requests | The requests running and waiting, for each instance, over time | The engine, by the names of the per-engine table |
| Promotion | The seconds each stage of a start took, for each launch that started a stopped instance | The lifecycle agent (section 5.2) |
| Cold launch | The same, for the launches of a new instance | The lifecycle agent |
| Instances, one line each | For each instance, now: GPU utilization, GPU memory used and power; the engine's running and waiting requests, its output tokens a second, and its time to first token, the median and the 95th percentile, with the requests behind them | The DCGM exporter; the engine, by the names of the per-engine table |

**The session's window.** The scaler sends the start and the end of a pool's session while the
session has not ended, booked or active: the panel reads them now, at the range's end, as dates,
and reads nothing for a pool with no session to come.

**The cause of the last failed launch** is a label: the scaler sends one series for each cause,
1 for the one in force and 0 for the others. The panel draws the one in force, named by its cause,
on an axis of its own; none is drawn while nothing waits or no launch has failed.

**A launch is an instance's series.** The lifecycle agent's gauge holds what each stage took in
the instance's last launch, and the collector labels it with the instance. So one series is one
stage of one launch: it starts when the stage ends and lasts while the instance reports. The two
transitions are two panels, never one: a cold launch may have the image to pull, and its seconds
are not a promotion's. A group keeps no stopped instance (section 4.5), so every start is a cold
launch and the promotion's panel has no new launch to show; the lifecycle agent still knows both.

**The decisions are counted over a window of their own.** The scaler sends one sample a minute.
`rules.WINDOW` can be as short as one minute, which holds one sample at most times, and an
increase needs two. The increase is still taken for each series and then summed.

**One line per instance** (`rules.engine_table`). Each column is one query of the value now, with
one series for each instance: a GPU's utilization is the mean of the instance's GPUs, its memory
used and its power their sum (the GPU dashboard shows each GPU), and a rate is taken for each
series and then summed for the instance. A mock instance has no GPU: the table of a pool whose
engine is the mock has the engine's columns only.

The engine's panel and the table follow the rule of a pool with no instance
(`rules.engine_timeseries`): they read "no session" while the pool's session is not active, and
"waiting for capacity" while it is active and the scaler counts no instance in service. The
scaler's own numbers and a launch's seconds are not engine series, and are shown as they are.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from grafana_foundation_sdk.builders import common as common_builders
from grafana_foundation_sdk.builders import dashboard as dashboard_builders
from grafana_foundation_sdk.builders import stat as stat_builders
from grafana_foundation_sdk.builders import timeseries as timeseries_builders
from grafana_foundation_sdk.models import common
from grafana_foundation_sdk.models import dashboard as models

from rhi_common.engine_metrics import MOCK, Quantity
from rhi_dashboards import gpu, objectives, rules

if TYPE_CHECKING:
    from grafana_foundation_sdk.builders import table as table_builders

    from rhi.config import Config
    from rhi_common.pool_config import Pool

UID = "fleet"
TITLE = "Fleet and scaling"

SCALER = "services/scaler/rhi_scaler/metrics.py"
LIFECYCLE = "services/engine/lifecycle/rhi_lifecycle/stages.py"

# The scaler's counts of a pool's instances, in the row's order, each with the name its series is read by.
COUNTS = {
    "requested": "rhi_scaler_instances_requested",
    "asked of AWS": "rhi_scaler_desired_capacity",
    "waiting for capacity": "rhi_scaler_instances_waiting",
    "starting": "rhi_scaler_instances_starting",
    "in service": rules.IN_SERVICE,
}
# The window of the pool's session, in seconds since the epoch, each with the name it is read by.
SESSION_WINDOW = {
    "starts": "rhi_scaler_session_starts_at_seconds",
    "ends": "rhi_scaler_session_ends_at_seconds",
}
# What the session's gauge reads as, by its value.
SESSION_READS = {"1": "active", "0": "not active"}
# How long the pool has waited for the instances it asked of AWS, and why its last launch failed:
# one series for each cause, 1 for the one in force.
WAITING_SECONDS = "rhi_scaler_waiting_seconds"
LAUNCH_FAILURE = "rhi_scaler_launch_failure"
CAUSE = "cause"
# The scaler's count of its decisions, labelled by reason.
DECISIONS = "rhi_scaler_decisions_total"
REASON = "reason"
# Five of the scaler's samples, one a minute.
DECISIONS_WINDOW = "5m"

# The engine's load as the scaler reads it, each with the name its series is read by.
ENGINE_LOAD = {"running": Quantity.RUNNING_REQUESTS, "waiting": Quantity.WAITING_REQUESTS}
# The label the collector gives every series of an instance: the instance's ID.
INSTANCE = rules.INSTANCE

# What each stage of an instance's last launch took, labelled by transition and by stage.
STAGE_SECONDS = "rhi_lifecycle_stage_seconds"
TRANSITION = "transition"
STAGE = "stage"
# The two transitions that put an instance in service, each with its panel's title.
STARTS = {
    "promotion": "Promotion of a stopped instance: seconds per stage, per launch",
    "cold-launch": "Cold launch: seconds per stage, per launch",
}

# The table's columns of an instance's GPUs, in its order: the header, the exporter's field, how
# the instance's GPUs are read as one, and the unit.
GPU_COLUMNS = (
    ("GPU utilization", gpu.COMPUTE, "avg", "percentunit"),
    ("GPU memory used", gpu.MEMORY_USED, "sum", "bytes"),
    ("GPU power", gpu.POWER, "sum", "watt"),
)
# Its columns of the instance's engine, after those.
RUNNING_COLUMN = "Running requests"
WAITING_COLUMN = "Waiting requests"
OUTPUT_COLUMN = "Output tokens a second"
FIRST_TOKEN_COLUMNS = {0.5: "Time to first token, median", 0.95: "Time to first token, 95th percentile"}
FIRST_TOKEN_COUNT_COLUMN = "Requests behind the time to first token"
INSTANCES_TITLE = "Instances, one line each: GPU and engine telemetry, now"

# The objectives' row declares its own, and a name both declare is listed once.
METRICS: tuple[rules.Declared, ...] = tuple(
    dict.fromkeys(
        (
            *(rules.EmittedBy(name, SCALER) for name in (*COUNTS.values(), *SESSION_WINDOW.values())),
            *(rules.EmittedBy(name, SCALER) for name in (WAITING_SECONDS, LAUNCH_FAILURE, DECISIONS)),
            *(rules.FromEngineTable(quantity) for quantity in ENGINE_LOAD.values()),
            rules.FromEngineTable(Quantity.OUTPUT_TOKENS),
            rules.FromEngineTable(Quantity.TIME_TO_FIRST_TOKEN),
            *(rules.EmittedBy(field, gpu.COUNTERS) for _, field, _, _ in GPU_COLUMNS),
            rules.EmittedBy(STAGE_SECONDS, LIFECYCLE),
            *objectives.METRICS,
        )
    )
)

# A row is 24 columns wide: the session and the instances on its first line, three panels on its
# second, two on its third, and the table on its last.
QUARTER = 6
THIRD = 8
HALF = 12
WHOLE = 24
# The objectives' row: four stats to a line.
OBJECTIVES_ROW = "Objectives, each pool's while its session is active"
# The references of a measure's query and of the count behind it.
MEASURE_REF = "A"
COUNT_REF = "B"
# What a measure whose buckets do not settle its objective reads, and its colour.
INCONCLUSIVE = "inconclusive"
INCONCLUSIVE_COLOR = "orange"
SESSION_TITLE = "Session: whether it is active, its start and its end"
COUNTS_TITLE = "Instances: requested, asked of AWS, waiting for capacity, starting, in service"
WAITING_TITLE = "Waiting for capacity: seconds waited, and the cause of the last failed launch"
MILLISECONDS = 1000


def _graph(title: str) -> timeseries_builders.Panel:
    """A graph whose legend lists each series with its last value."""
    legend = (
        common_builders.VizLegendOptions()
        .show_legend(True)
        .display_mode(common.LegendDisplayMode.TABLE)
        .calcs(["lastNotNull"])
    )
    return timeseries_builders.Panel().title(title).datasource(rules.PROMETHEUS).legend(legend)


def _queries(panel: timeseries_builders.Panel, queries: dict[str, str]) -> timeseries_builders.Panel:
    """Adds one query for each legend of `queries`, in their order."""
    for index, (legend, expression) in enumerate(queries.items()):
        panel.with_target(rules.query(expression, legend, chr(ord("A") + index)))
    return panel


def session(pool: Pool) -> stat_builders.Panel:
    """Whether the pool's session is active, and the session's start and end, as the scaler reads them now."""
    matchers = {rules.POOL: pool.name}
    panel = (
        stat_builders.Panel()
        .title(SESSION_TITLE)
        .datasource(rules.PROMETHEUS)
        .reduce_options(common_builders.ReduceDataOptions().calcs(["lastNotNull"]))
        .text_mode(common.BigValueTextMode.VALUE_AND_NAME)
        .with_target(rules.query(rules.selector(rules.SESSION_ACTIVE, matchers), "session", "A", instant=True))
    )
    reads = {value: models.ValueMappingResult(text=text, index=index) for index, (value, text) in enumerate(SESSION_READS.items())}
    panel.override_by_query("A", [models.DynamicConfigValue(id_val="mappings", value=[models.ValueMap(options=reads)])])
    for index, (legend, metric) in enumerate(SESSION_WINDOW.items(), start=1):
        # Grafana reads a date in milliseconds since the epoch.
        ref = chr(ord("A") + index)
        panel.with_target(rules.query(f"{rules.selector(metric, matchers)} * {MILLISECONDS}", legend, ref, instant=True))
        panel.override_by_query(ref, [models.DynamicConfigValue(id_val="unit", value="dateTimeAsIso")])
    return panel


def counts(pool: Pool) -> timeseries_builders.Panel:
    """The instances the pool's session requests, those asked of AWS, and those waiting for capacity, starting and in service."""
    queries = {legend: rules.selector(metric, {rules.POOL: pool.name}) for legend, metric in COUNTS.items()}
    return _queries(_graph(COUNTS_TITLE), queries).decimals(0).min(0)


def waiting(pool: Pool) -> timeseries_builders.Panel:
    """The seconds the pool has waited for capacity, and the cause of its last failed launch while it is in force."""
    matchers = {rules.POOL: pool.name}
    queries = {
        "seconds waited": rules.selector(WAITING_SECONDS, matchers),
        f"last failed launch: {{{{{CAUSE}}}}}": f"{rules.selector(LAUNCH_FAILURE, matchers)} == 1",
    }
    panel = _queries(_graph(WAITING_TITLE), queries).unit("s").min(0)
    # The cause is a label, not seconds: its series is read by its name, on an axis of its own.
    return panel.override_by_query(
        "B",
        [
            models.DynamicConfigValue(id_val="custom.axisPlacement", value="right"),
            models.DynamicConfigValue(id_val="unit", value="none"),
        ],
    )


def decisions(pool: Pool) -> timeseries_builders.Panel:
    """The scaler's decisions for the pool over the window, counted by reason."""
    counted = rules.increase(DECISIONS, {rules.POOL: pool.name}, window=DECISIONS_WINDOW, by=(REASON,))
    title = f"Scale decisions by reason, over {DECISIONS_WINDOW}"
    return _queries(_graph(title), {f"{{{{{REASON}}}}}": counted}).min(0)


def engine_load(pool: Pool) -> timeseries_builders.Panel:
    """The requests each instance of the pool runs and holds waiting, as its engine reports them."""
    queries = {
        f"{legend} {{{{{INSTANCE}}}}}": f"sum by ({rules.POOL}, {INSTANCE}) ({rules.engine_selector(pool, quantity)})"
        for legend, quantity in ENGINE_LOAD.items()
    }
    return rules.engine_timeseries(pool, "Engine requests: running and waiting, per instance", queries).min(0)


def instances(pool: Pool) -> table_builders.Panel:
    """One line for each instance of the pool: its GPUs' telemetry, unless the pool's engine is the
    mock, which has no GPU, then its engine's."""
    by_instance = (INSTANCE,)
    columns: list[rules.Column] = []
    if pool.engine != MOCK:
        for header, field, aggregation, unit in GPU_COLUMNS:
            series = rules.selector(field, {rules.POOL: pool.name})
            columns.append(rules.Column(header, f"{aggregation} by ({rules.POOL}, {INSTANCE}) ({series})", unit))
    for header, quantity in ((RUNNING_COLUMN, Quantity.RUNNING_REQUESTS), (WAITING_COLUMN, Quantity.WAITING_REQUESTS)):
        load = f"sum by ({rules.POOL}, {INSTANCE}) ({rules.engine_selector(pool, quantity)})"
        columns.append(rules.Column(header, load, "short"))
    columns.append(rules.Column(OUTPUT_COLUMN, rules.engine_rate(pool, Quantity.OUTPUT_TOKENS, by=by_instance), "short"))
    first_token = Quantity.TIME_TO_FIRST_TOKEN
    for q, header in FIRST_TOKEN_COLUMNS.items():
        columns.append(rules.Column(header, rules.engine_quantile(pool, q, first_token, by=by_instance), "s"))
    # A percentile is shown with the requests behind it, over the same window.
    behind = rules.engine_request_count(pool, first_token, by=by_instance)
    columns.append(rules.Column(FIRST_TOKEN_COUNT_COLUMN, behind, "short"))
    return rules.engine_table(pool, INSTANCES_TITLE, columns)


def stage_seconds(pool: Pool, transition: str) -> timeseries_builders.Panel:
    """The seconds each stage took in each launch of the pool that went through `transition`."""
    launches = rules.selector(STAGE_SECONDS, {rules.POOL: pool.name, TRANSITION: transition})
    # One series for each instance and stage, whatever other label a series carries.
    per_launch = f"max by ({rules.POOL}, {INSTANCE}, {STAGE}) ({launches})"
    return _queries(_graph(STARTS[transition]), {f"{{{{{INSTANCE}}}}} {{{{{STAGE}}}}}": per_launch}).unit("s").min(0)


def _target(measure: objectives.Measure) -> dashboard_builders.ThresholdsConfig:
    """Red, then green from the least value that meets the measure's threshold."""
    steps = [
        models.Threshold(value=None, color="red"),
        models.Threshold(value=objectives.threshold_step(measure), color="green"),
    ]
    return dashboard_builders.ThresholdsConfig().mode(models.ThresholdsMode.ABSOLUTE).steps(steps)


def _reads_inconclusive() -> list[models.DynamicConfigValue]:
    """`objectives.INCONCLUSIVE` displayed as the text "inconclusive", neither red nor green."""
    result = models.ValueMappingResult(text=INCONCLUSIVE, color=INCONCLUSIVE_COLOR, index=0)
    mapping = models.ValueMap(options={f"{objectives.INCONCLUSIVE:g}": result})
    return [models.DynamicConfigValue(id_val="mappings", value=[mapping])]


def objective_stat(objective: objectives.Objective, measure: objectives.Measure) -> stat_builders.Panel:
    """One measure of an objective over the time its session was active in the range, coloured by its target."""
    whose = "any pool's session" if measure.pool is rules.ANY_POOL else f"the session of the pool {measure.pool}"
    panel = (
        stat_builders.Panel()
        .title(f"{objective.name}: {measure.legend}")
        .description(f"Target: {measure.goal}, over the time {whose} was active")
        .datasource(rules.PROMETHEUS)
        .reduce_options(common_builders.ReduceDataOptions().calcs(["lastNotNull"]))
        .unit(measure.unit)
        .thresholds(_target(measure))
        .color_mode(common.BigValueColorMode.VALUE)
        .with_target(rules.query(measure.expression, measure.legend, MEASURE_REF, instant=True))
    )
    if measure.ceiling is not None:
        # Buckets that bound the share on both sides of its threshold settle nothing.
        panel.override_by_query(MEASURE_REF, _reads_inconclusive())
    if measure.count is not None:
        panel.with_target(rules.query(measure.count, "observed", COUNT_REF, instant=True))
        # A count has neither the measure's unit nor its target.
        panel.override_by_query(
            COUNT_REF,
            [
                models.DynamicConfigValue(id_val="unit", value="short"),
                models.DynamicConfigValue(id_val="color", value={"mode": "fixed", "fixedColor": "text"}),
            ],
        )
    return panel


def dashboard(config: Config) -> dashboard_builders.Dashboard:
    """The dashboard: the objectives' row, then one row for each pool of the configuration, in its order."""
    built = dashboard_builders.Dashboard(TITLE).uid(UID)
    built.with_row(dashboard_builders.Row(OBJECTIVES_ROW))
    for objective in objectives.objectives(config):
        for measure in objective.measures:
            built.with_panel(objective_stat(objective, measure).span(QUARTER))
    for pool in config.pools:
        built.with_row(dashboard_builders.Row(pool.name))
        built.with_panel(session(pool).span(QUARTER))
        built.with_panel(counts(pool).span(WHOLE - QUARTER))
        for panel in (waiting(pool), decisions(pool), engine_load(pool)):
            built.with_panel(panel.span(THIRD))
        for transition in STARTS:
            built.with_panel(stage_seconds(pool, transition).span(HALF))
        # Under these, one line per instance.
        built.with_panel(instances(pool).span(WHOLE))
    return built
