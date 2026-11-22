"""The cost dashboard: GPU-hours and estimated dollars per pool, the estimated cost per million
output tokens, and the month's estimate beside the account's bill.

One of the six dashboards of the system design (section 7.3). One row for each pool of the
configuration, of six panels, then one row of the bill:

| Panel | Window | Shows |
| --- | --- | --- |
| GPU-hours today | The day so far | The hours of the pool's running instances, warm-pool preparation included |
| GPU-hours this month | The month so far | The same |
| Cost today | The day so far | Those hours at the pool's configured hourly price |
| Cost this month | The month so far | The same |
| Cost per million output tokens | The dashboard's time range | That cost over the output tokens the gateway settled for the pool |
| Cost per million output tokens, this month | The month so far | The same |
| The bill (last row, one panel) | The month so far | The account's month-to-date charge, beside the month's estimate over every pool |

**Every number computed here is an estimate**, and its panel's title says so (section 7.2): the
instance count times a price the configuration gives is not what AWS bills. The one number that
is not computed is the bill's, which the last panel shows beside the estimate, so the two can be
reconciled.

**The hours are Auto Scaling's count of the pool's running instances**, from the group metrics
`rhi.pools.group` turns on, in the namespace `AWS/AutoScaling`: `GroupTotalInstances` (in service,
pending and terminating) plus `WarmPoolPendingCapacity` (an instance prepared for the warm pool
runs before it stops), as the fleet's alarms read them (`rhi.guardrails.alarms_fleet`). A group
without a warm pool has no warm-pool point, which adds nothing. Each series has one data point a
minute, the count during that minute, so the `Sum` statistic over a period is the instance-minutes
inside it. A period cut short by the window's edge, the hour that is still running included, holds
only its own minutes: nothing counts a partial hour as a whole one. Grafana's server-side
expressions sum each count over the window, add the two, and divide by 60: the instance-hours.

**A pool's group is read by its name**, which the pools stack fixes (`rhi_common.pool_config.group_name`):
each count is one metric, read by its namespace, name and the one dimension `AutoScalingGroupName`
with that value exactly, which CloudWatch answers with every data point it keeps in the window. A
group deleted and created again, whose hours the pool also ran, published under the same name, so
its hours are counted too. No query searches for the group: a search finds only the metrics that
reported in the past two weeks.

**The price is the hourly price the configuration gives the pool's first instance type**, the one
the group launches first (section 5.4): a fallback type launched in its place is counted at that
price too. The dollars are the hours times it.

**The tokens are the output tokens the gateway settled for the pool**
(`rhi_gateway_student_output_tokens_total`, `rhi_gateway.metrics`): an instance's price is over what
it produced, its output (section 4.7). Each series is counted on its own, then summed by pool
(section 7.2, first rule). A series that was there at the window's start counts its increase over
the window. A series the gateway creates inside the window, on its first settlement for an alias
(a new alias, a new task), has no sample before that settlement, so its increase would leave the
settlement out, and a first settlement followed by none would count nothing: such a series counts
its latest value instead, every token it settled since it was created. Grafana's server-side
expressions divide the dollars, from CloudWatch, by the tokens, from Prometheus, in millions.

**The calendar windows are the panel's own**: "today" is from the start of the day to now, "this
month" from the start of the month to now, in UTC, as AWS bills. Grafana gives a panel its own
window only while the dashboard's time range is relative, as its default, this month so far, is:
under a range of fixed dates (a past session's start and end), Grafana ignores a panel's window
(its query options, "Relative time"), and every query Grafana makes is bounded by the range it is
given, so no panel can read the day or the month then. Each calendar panel's header names its
window ("Today so far", "This month so far") whenever Grafana applies it, and its description says
so.

**The bill** is CloudWatch's billing metric, `EstimatedCharges` in `AWS/Billing` with the one
dimension `Currency`, `USD`: the account's charge since the month began. AWS publishes it in US
East (N. Virginia) only, whatever region the endpoint is in, so that query names the region
`us-east-1`; the instances' queries read the data source's own region, the endpoint's. AWS
publishes it only once billing alerts are on, the owner's fifth one-time step (section 8.6). It
holds what the instances' estimate leaves out: the always-on platform, about $170 a month at idle
(section 11.2), storage, traffic. AWS updates it several times a day, and may lower it: the panel
shows its latest reading, not its highest.

**No panel shows a ceiling**: there are no GPU-hour ceilings (section 11.3). Every panel has one
colour: its thresholds hold the base step alone, where Grafana's default would turn red at 80.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from grafana_foundation_sdk.builders import cloudwatch as cloudwatch_builders
from grafana_foundation_sdk.builders import common as common_builders
from grafana_foundation_sdk.builders import dashboard as dashboard_builders
from grafana_foundation_sdk.builders import expr as expr_builders
from grafana_foundation_sdk.builders import stat as stat_builders
from grafana_foundation_sdk.models import cloudwatch, common
from grafana_foundation_sdk.models import dashboard as models

from rhi_common.pool_config import group_name
from rhi_dashboards import rules

if TYPE_CHECKING:
    from rhi.config import Config
    from rhi_common.pool_config import Pool

UID = "cost"
TITLE = "Cost"

# The gateway's output tokens settled, by pool; a counter's samples end in `_total`.
GATEWAY_METRICS = "services/gateway/rhi_gateway/metrics.py"
OUTPUT_TOKENS = "rhi_gateway_student_output_tokens"
METRICS: tuple[rules.Declared, ...] = (rules.EmittedBy(OUTPUT_TOKENS, GATEWAY_METRICS),)

# Auto Scaling's group metrics: the namespace, the group's dimension, and the two counts whose sum
# is the instances running, each with the letter of its query.
AUTO_SCALING = "AWS/AutoScaling"
GROUP_DIMENSION = "AutoScalingGroupName"
INSTANCE_COUNTS = {"GroupTotalInstances": "T", "WarmPoolPendingCapacity": "W"}
# The period the counts are summed over, in seconds: a whole number of minutes, and of the five
# minutes CloudWatch keeps one-minute data at after 15 days.
PERIOD = 3600
MINUTES_AN_HOUR = 60

# The billing metric: where AWS publishes it, its namespace, name and dimension, and how often.
BILLING_REGION = "us-east-1"
BILLING = "AWS/Billing"
CHARGES = "EstimatedCharges"
CURRENCY: cloudwatch.Dimensions = {"Currency": "USD"}
# An hour: one reading or none in most periods, so that a period's maximum is its reading.
BILLING_PERIOD = 3600
# The data source's own region: the endpoint's.
OWN_REGION = "default"

# The calendar windows, as Grafana's relative times, from their start to now; and the time zone
# they are taken in: AWS bills by the month in UTC.
TODAY = "now/d"
THIS_MONTH = "now/M"
TIME_ZONE = "utc"
# The panel's window, as Grafana gives it to a Prometheus query.
WINDOW = "$__range"

# Every number is one colour, with no threshold: nothing is a ceiling.
COLOR = "blue"
USD = "currencyUSD"
COUNT = "none"
SIXTH = 4
HALF = 12
BILLING_ROW = "The bill, month to date"


def price(pool: Pool) -> float:
    """Dollars an hour: the configured price of the pool's first instance type."""
    return pool.prices[pool.instance_types[0]]


def metric(namespace: str, name: str, dimensions: cloudwatch.Dimensions, ref_id: str) -> cloudwatch_builders.MetricsQuery:
    """One metric of the CloudWatch data source, read by its namespace, name and dimensions exactly,
    hidden: an expression computes from it."""
    return (
        cloudwatch_builders.MetricsQuery()
        .query_mode(cloudwatch.QueryMode.METRICS)
        .metric_query_type(cloudwatch.MetricQueryType.SEARCH)
        .metric_editor_mode(cloudwatch.MetricEditorMode.BUILDER)
        .namespace(namespace)
        .metric_name(name)
        .dimensions(dimensions)
        .match_exact(True)
        .id(ref_id.lower())
        .ref_id(ref_id)
        .hide(True)
        .datasource(rules.CLOUDWATCH)
    )


def count(pool: Pool, name: str, ref_id: str) -> cloudwatch_builders.MetricsQuery:
    """One count of the pool's group, in the endpoint's region: its instance-minutes in each period."""
    dimensions: cloudwatch.Dimensions = {GROUP_DIMENSION: group_name(pool.name)}
    return metric(AUTO_SCALING, name, dimensions, ref_id).statistic("Sum").region(OWN_REGION).period(str(PERIOD))


def charges(ref_id: str) -> cloudwatch_builders.MetricsQuery:
    """The account's charge since the month began, in the one region AWS publishes it in."""
    query = metric(BILLING, CHARGES, CURRENCY, ref_id)
    return query.statistic("Maximum").region(BILLING_REGION).period(str(BILLING_PERIOD))


# The reductions a server-side expression is asked for.
type Reducer = Literal["sum", "last"]


def reduce(ref_id: str, of: str, reducer: Reducer, *, hide: bool = True) -> expr_builders.TypeReduce:
    """A server-side expression: the query `of` reduced to one number, what is not a number left
    out, shown unless `hide`."""
    return (
        expr_builders.TypeReduce()
        .expression(f"${of}")
        .reducer(reducer)
        .settings(expr_builders.ExprTypeReduceSettings().mode("dropNN"))
        .ref_id(ref_id)
        .hide(hide)
        .datasource(rules.EXPRESSIONS)
    )


def math(ref_id: str, expression: str, *, hide: bool = False) -> expr_builders.TypeMath:
    """A server-side expression over the panel's numbers, shown unless `hide`."""
    return expr_builders.TypeMath().expression(expression).ref_id(ref_id).hide(hide).datasource(rules.EXPRESSIONS)


type Target = cloudwatch_builders.MetricsQuery | expr_builders.TypeReduce


def hours(pool: Pool, suffix: str = "") -> tuple[list[Target], str]:
    """The queries of the pool's two counts, each summed over the window, and the server-side
    expression of the hours they add up to. `suffix` tells one pool's queries from another's."""
    targets: list[Target] = []
    sums = []
    for name, letter in INSTANCE_COUNTS.items():
        targets += [count(pool, name, letter + suffix), reduce(f"S{letter}{suffix}", letter + suffix, "sum")]
        sums.append(f"$S{letter}{suffix}")
    return targets, f"({' + '.join(sums)}) / {MINUTES_AN_HOUR}"


def dollars(pool: Pool, suffix: str = "") -> tuple[list[Target], str]:
    """The same queries, and the expression of those hours at the pool's price, in dollars."""
    targets, expression = hours(pool, suffix)
    return targets, f"{expression} * {price(pool)!r}"


def output_tokens(pool: Pool) -> str:
    """The output tokens the gateway settled for the pool over the panel's window, read at its end:
    the increase of each series there at the window's start, and the latest value of each series
    created inside it, which counts its first settlement, summed by pool."""
    series = rules.selector(OUTPUT_TOKENS + rules.COUNTER_SAMPLE, {rules.POOL: pool.name})
    at_start = f"{series} offset {WINDOW}"
    there = f"sum by ({rules.POOL}) (increase({series}[{WINDOW}]) and {at_start})"
    created = f"sum by ({rules.POOL}) (last_over_time({series}[{WINDOW}]) unless {at_start})"
    return f"(({there}) + ({created})) or ({there}) or ({created})"


def _one_colour() -> dashboard_builders.ThresholdsConfig:
    """One step, the base: no value is ever shown against a threshold."""
    steps = [models.Threshold(value=None, color=COLOR)]
    return dashboard_builders.ThresholdsConfig().mode(models.ThresholdsMode.ABSOLUTE).steps(steps)


def _stat(title: str, description: str, unit: str) -> stat_builders.Panel:
    """A panel of the numbers its server-side expressions give, each its own."""
    return (
        stat_builders.Panel()
        .title(title)
        .description(description)
        .unit(unit)
        .decimals(2 if unit == USD else 1)
        .thresholds(_one_colour())
        .color_mode(common.BigValueColorMode.NONE)
        .graph_mode(common.BigValueGraphMode.NONE)
        .reduce_options(common_builders.ReduceDataOptions().calcs(["lastNotNull"]))
    )


def _with[P: stat_builders.Panel](panel: P, targets: list[Target]) -> P:
    for target in targets:
        panel.with_target(target)
    return panel


_CALENDAR = {
    TODAY: ("today", "from the start of the day, UTC"),
    THIS_MONTH: ("this month", "from the start of the month, UTC"),
}
_FIXED_RANGE = "Grafana applies it while the dashboard's time range is relative, and ignores it under fixed dates."


def hours_stat(pool: Pool, window: str) -> stat_builders.Panel:
    """The pool's GPU-hours over a calendar window."""
    name, since = _CALENDAR[window]
    description = (
        f"The hours of the pool's running instances, warm-pool preparation included, {since} to now: "
        f"Auto Scaling's {' plus '.join(INSTANCE_COUNTS)}. {_FIXED_RANGE}"
    )
    targets, expression = hours(pool)
    panel = _stat(f"GPU-hours {name}, estimate", description, COUNT).datasource(rules.CLOUDWATCH)
    return _with(panel.time_from(window), targets).with_target(math("H", expression))


def dollars_stat(pool: Pool, window: str) -> stat_builders.Panel:
    """The pool's estimated cost over a calendar window."""
    name, since = _CALENDAR[window]
    description = (
        f"The GPU-hours {since} to now, at ${price(pool)!r} an hour, the configured price of "
        f"{pool.instance_types[0]}. {_FIXED_RANGE}"
    )
    targets, expression = dollars(pool)
    panel = _stat(f"Cost {name}, estimate", description, USD).datasource(rules.CLOUDWATCH)
    return _with(panel.time_from(window), targets).with_target(math("E", expression))


def per_million_stat(pool: Pool, window: str | None) -> stat_builders.Panel:
    """The pool's estimated cost per million output tokens: over the dashboard's time range, or a
    calendar window."""
    if window is None:
        title, over = "Cost per million output tokens, dashboard's range, estimate", "over the dashboard's time range"
    else:
        name, since = _CALENDAR[window]
        title, over = f"Cost per million output tokens, {name}, estimate", f"{since} to now. {_FIXED_RANGE}"
    description = (
        f"The estimated cost of the pool's instances, at ${price(pool)!r} an hour, over the output tokens "
        f"the gateway settled for the pool, in millions, {over}"
    )
    targets, expression = dollars(pool)
    panel = _with(_stat(title, description, USD).datasource(rules.MIXED), targets)
    panel.with_target(rules.query(output_tokens(pool), "tokens", "B", instant=True).hide(True))
    panel.with_target(reduce("D", "B", "last"))
    panel.with_target(math("E", f"{expression} / ($D / 1000000)"))
    return panel if window is None else panel.time_from(window)


def bill_stat(config: Config) -> stat_builders.Panel:
    """The account's month-to-date charge, beside the month's estimate over every pool."""
    description = (
        "The account's charge since the month began, from CloudWatch's billing metric in US East "
        "(N. Virginia), beside the estimated cost of every pool's instances over the same month, "
        f"in UTC. The bill also holds the always-on platform, storage and traffic. {_FIXED_RANGE}"
    )
    panel = (
        _stat("The bill beside the estimate, month to date", description, USD)
        .datasource(rules.CLOUDWATCH)
        .time_from(THIS_MONTH)
        .with_target(charges("A"))
        # The latest reading: AWS may lower its estimate of the month.
        .with_target(reduce("C", "A", "last", hide=False))
        .override_by_query("C", [models.DynamicConfigValue(id_val="displayName", value="AWS charge, month to date")])
    )
    estimates = []
    for index, pool in enumerate(config.pools):
        targets, expression = dollars(pool, str(index))
        _with(panel, targets)
        estimates.append(f"({expression})")
    # With no pool, there is no instance to estimate.
    if estimates:
        panel.with_target(math("D", " + ".join(estimates)))
        shown = [models.DynamicConfigValue(id_val="displayName", value="Instances, month to date, estimate")]
        panel.override_by_query("D", shown)
    return panel


def dashboard(config: Config) -> dashboard_builders.Dashboard:
    """The dashboard: one row for each pool of the configuration, in its order, then the bill."""
    built = dashboard_builders.Dashboard(TITLE).uid(UID).timezone(TIME_ZONE).time(THIS_MONTH, "now")
    for pool in config.pools:
        built.with_row(dashboard_builders.Row(pool.name))
        for panel in (
            hours_stat(pool, TODAY),
            hours_stat(pool, THIS_MONTH),
            dollars_stat(pool, TODAY),
            dollars_stat(pool, THIS_MONTH),
            per_million_stat(pool, None),
            per_million_stat(pool, THIS_MONTH),
        ):
            built.with_panel(panel.span(SIXTH))
    built.with_row(dashboard_builders.Row(BILLING_ROW))
    built.with_panel(bill_stat(config).span(HALF))
    return built
