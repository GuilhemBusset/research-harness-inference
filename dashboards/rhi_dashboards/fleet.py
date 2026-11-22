"""The fleet and scaling dashboard: what the scaler decided and why, and what each start cost.

The first of the six dashboards of the system design (section 7.3). Its first row shows the five
objectives of section 7.3 (`rhi_dashboards.objectives`), each against its target, over the minutes
a session was on in the dashboard's time range: one stat per measure, green where it meets its
target and red where it does not, with the count of observations behind a percentile beside it. A
percentile is shown as the share of observations it is judged on (`rhi_dashboards.objectives`), and
a value that must be above its threshold turns green only above it: at the threshold it is red.
Each is one value for the whole range, so its queries are instant ones, read at the range's end over
the range (`$__range`); a range with no session reads no data.

Then one row for each pool of the configuration, of five panels:

| Panel | Shows | From |
| --- | --- | --- |
| Instances | The desired capacity, the instances in service and the instances in the warm pool | The scaler's gauges (section 7.2) |
| Scale decisions | The decisions made, counted by reason | The scaler's counter |
| Engine requests | The requests running and waiting, for each instance: the scaler's view of the engine's load (section 5.1) | The engine, by the names of the per-engine table |
| Start from the warm pool | The seconds each stage of a start took, for each launch | The lifecycle agent (section 5.2) |
| Cold launch | The same, for the launches that had no warm instance to promote | The lifecycle agent |

**A launch is an instance's series.** The lifecycle agent's gauge holds what each stage took in
the instance's last launch, and the collector labels it with the instance. So one series is one
stage of one launch: it starts when the stage ends and lasts while the instance reports. The two
transitions are two panels, never one: a cold launch may have the image to pull, and its seconds
beside a promotion's would hide the number the design is judged on, the start from the warm pool.
An entry to the warm pool starts no engine and is in neither.

**The decisions are counted over a window of their own.** The scaler sends one sample a minute.
`rules.WINDOW` can be as short as one minute, which holds one sample at most times, and an
increase needs two. The increase is still taken for each series and then summed.

The engine's panel follows the rule of a pool at zero (`rules.engine_timeseries`): it reads
"asleep" while the scaler counts no instance in service. The scaler's own numbers and a launch's
seconds are not engine series, and are shown as they are.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from grafana_foundation_sdk.builders import common as common_builders
from grafana_foundation_sdk.builders import dashboard as dashboard_builders
from grafana_foundation_sdk.builders import stat as stat_builders
from grafana_foundation_sdk.builders import timeseries as timeseries_builders
from grafana_foundation_sdk.models import common
from grafana_foundation_sdk.models import dashboard as models

from rhi_common.engine_metrics import Quantity
from rhi_dashboards import objectives, rules

if TYPE_CHECKING:
    from rhi.config import Config
    from rhi_common.pool_config import Pool

UID = "fleet"
TITLE = "Fleet and scaling"

SCALER = "services/scaler/rhi_scaler/metrics.py"
LIFECYCLE = "services/engine/lifecycle/rhi_lifecycle/stages.py"

# The scaler's gauges for a pool, each with the name its series is read by.
COUNTS = {
    "desired": "rhi_scaler_desired_capacity",
    "in service": rules.IN_SERVICE,
    "warm": "rhi_scaler_warm_instances",
}
# The scaler's count of its decisions, labelled by reason.
DECISIONS = "rhi_scaler_decisions_total"
REASON = "reason"
# Five of the scaler's samples, one a minute.
DECISIONS_WINDOW = "5m"

# The engine's load as the scaler reads it, each with the name its series is read by.
ENGINE_LOAD = {"running": Quantity.RUNNING_REQUESTS, "waiting": Quantity.WAITING_REQUESTS}
# The label the collector gives every series of an instance: the instance's ID.
INSTANCE = "instance"

# What each stage of an instance's last launch took, labelled by transition and by stage.
STAGE_SECONDS = "rhi_lifecycle_stage_seconds"
TRANSITION = "transition"
STAGE = "stage"
# The two transitions that put an instance in service, each with its panel's title.
STARTS = {
    "promotion": "Start from the warm pool: seconds per stage, per launch",
    "cold-launch": "Cold launch: seconds per stage, per launch",
}

# The objectives' row declares its own; the lifecycle agent's gauge is in both, and listed once.
METRICS: tuple[rules.Declared, ...] = tuple(
    dict.fromkeys(
        (
            *(rules.EmittedBy(name, SCALER) for name in COUNTS.values()),
            rules.EmittedBy(DECISIONS, SCALER),
            *(rules.FromEngineTable(quantity) for quantity in ENGINE_LOAD.values()),
            rules.EmittedBy(STAGE_SECONDS, LIFECYCLE),
            *objectives.METRICS,
        )
    )
)

# A row is 24 columns wide: three panels on its first line, two on its second.
THIRD = 8
HALF = 12
# The objectives' row: four stats to a line.
QUARTER = 6
OBJECTIVES_ROW = "Objectives, while a session is on"
# The references of a measure's query and of the count behind it.
MEASURE_REF = "A"
COUNT_REF = "B"


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


def counts(pool: Pool) -> timeseries_builders.Panel:
    """The pool's desired capacity, its instances in service and its warm instances."""
    queries = {legend: rules.selector(metric, {rules.POOL: pool.name}) for legend, metric in COUNTS.items()}
    return _queries(_graph("Instances: desired, in service, warm"), queries).decimals(0).min(0)


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


def objective_stat(objective: objectives.Objective, measure: objectives.Measure) -> stat_builders.Panel:
    """One measure of an objective over the session's minutes in the range, coloured by its target."""
    panel = (
        stat_builders.Panel()
        .title(f"{objective.name}: {measure.legend}")
        .description(f"Target: {measure.goal}, over the minutes a session was on")
        .datasource(rules.PROMETHEUS)
        .reduce_options(common_builders.ReduceDataOptions().calcs(["lastNotNull"]))
        .unit(measure.unit)
        .thresholds(_target(measure))
        .color_mode(common.BigValueColorMode.VALUE)
        .with_target(rules.query(measure.expression, measure.legend, MEASURE_REF, instant=True))
    )
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
        for panel in (counts(pool), decisions(pool), engine_load(pool)):
            built.with_panel(panel.span(THIRD))
        for transition in STARTS:
            built.with_panel(stage_seconds(pool, transition).span(HALF))
    return built
