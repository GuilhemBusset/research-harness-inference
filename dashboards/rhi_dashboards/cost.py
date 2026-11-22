"""The cost dashboard: GPU-hours and estimated dollars per pool, the estimated cost per million
output tokens, and the month's estimate beside the account's bill.

One of the six dashboards of the system design (section 7.3), with a companion view of one of its
numbers over a session. The dashboard, "Cost", has one row for each pool of the configuration, of
five panels, then one row of the bill:

| Panel | Window | Shows |
| --- | --- | --- |
| GPU-hours today | The day so far | The hours of the pool's running instances, warm-pool preparation included |
| GPU-hours this month | The month so far | The same |
| Cost today | The day so far | Those hours at the pool's configured hourly price |
| Cost this month | The month so far | The same |
| Cost per million output tokens, this month | The month so far | That cost over the output tokens the gateway settled for the pool |
| The bill (last row, one panel) | The month so far | The account's month-to-date charge, beside the month's estimate over every pool |

Its companion, "Cost per session", has one panel for each pool: the cost per million output
tokens over its time range, the session chosen in its time picker.

**Every number computed here is an estimate**, and its panel's title says so (section 7.2): the
instance count times a price the configuration gives is not what AWS bills. The one number that
is not computed is the bill's, which the last panel shows beside the estimate, so the two can be
reconciled.

**The hours are Auto Scaling's count of the pool's running instances**, from the group metrics
`rhi.pools.group` turns on, in the namespace `AWS/AutoScaling`: `GroupTotalInstances` (in service,
pending and terminating) plus `WarmPoolPendingCapacity` (an instance prepared for the warm pool
runs before it stops), as the fleet's alarms read them (`rhi.guardrails.alarms_fleet`). Each series
has one data point a minute, the count during that minute, so the `Sum` statistic over a period is
the instance-minutes inside it. A period cut short by the window's edge, the hour that is still
running included, holds only its own minutes: nothing counts a partial hour as a whole one.

**A pool's group is read by its name**, which the pools stack fixes, `rhi-pool-<pool>`
(`rhi_common.pool_config.group_name`), and no other pool's group has: a group deleted and created
again, a parked stage's once unparked, publishes under the same name, so its hours are counted too.
Over the month, each count is one metric, read by its namespace, its name and the one dimension
`AutoScalingGroupName` with that value exactly, which CloudWatch answers with every data point it
keeps in the window: no search, which finds only the metrics that reported in the past two weeks.
Such a read always gives a series, one with no point where the group published none (the warm
pool's count of a group without one), which sums to zero. Grafana's server-side expressions sum
each count over the window, add the two, and divide by 60: the instance-hours.

**The price is the hourly price the configuration gives the pool's first instance type**, the one
the group launches first (section 5.4): a fallback type launched in its place is counted at that
price too. The dollars are the hours times it.

**The tokens are the output tokens the gateway settled for the pool**
(`rhi_gateway_student_output_tokens_total`, `rhi_gateway.metrics`): an instance's price is over what
it produced, its output (section 4.7). Each series is counted on its own, then summed (section 7.2,
first rule), by the samples Prometheus took inside the window, from its start to its end: what the
series rose by since its sample before, or all of its value where it had none in the five minutes
before or fell. The gateway creates a series on its first settlement for an alias (a new alias, a
new task), already holding those tokens, and removes it when the alias's key is revoked, to create
it again from zero if the alias is given a key: each time the series appears, or restarts lower,
its value is tokens settled since, and is counted. A series is read by its latest sample in the
five minutes up to each minute (`last_over_time`), not by Prometheus's instant value, so a series
that settled and was removed between two minutes is counted. At every minute of the window after
its start, the series' latest sample is compared with its latest a minute before, or, at the first
minute, with its latest at the window's start, so nothing settled before the start is counted and a
restart in that first minute keeps what the series settled after it; from the last minute to the
window's end, the same. A restart inside a minute leaves out what the series settled in that minute
before it. Grafana's server-side expressions divide the dollars, from CloudWatch, by the tokens,
from Prometheus, in millions.

**The calendar windows are the dashboard's own range**: "this month" is from the start of the month
to now, in UTC, as AWS bills, and "today" is the part of it from the start of the day. Grafana
gives every panel of a dashboard the dashboard's range once it is fixed dates (a past session's
start and end), whatever window a panel asks for (its query options, "Relative time"), and every
query is bounded by the range it is given. So the calendar panels never ask for a window of their
own: the dashboard's range is the month so far, its time picker is hidden, and the day is chosen
inside CloudWatch's answer, which has one point an hour from the start of the month: one query of
CloudWatch's metric math adds both counts of the pool's group, found by a search for its name
exactly (`SUM(SEARCH(...))`; a group that reported today reported in the past two weeks), and keeps
the points whose UTC day of the month is the last one of the range, today's
(`IF(DATE(x) == MAX(DATE(x)), x)`, the search written out for each `x`). Grafana sends each query of
a panel with server-side expressions to its data source on its own, so this query refers to no
other. A session is chosen in the
companion's time picker, where the only number is the one over the range chosen.

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
# is the instances running.
AUTO_SCALING = "AWS/AutoScaling"
GROUP_DIMENSION = "AutoScalingGroupName"
INSTANCE_COUNTS = ("GroupTotalInstances", "WarmPoolPendingCapacity")
# The period the counts are summed over, in seconds: a whole number of minutes, and of the five
# minutes CloudWatch keeps one-minute data at after 15 days.
PERIOD = 3600
MINUTES_AN_HOUR = 60
# Both counts of the group of that name exactly, added: the instance-minutes of each period.
RUNNING = "SUM(SEARCH('{{{namespace},{dimension}}} {dimension}=\"{group}\" ({metrics})', 'Sum', {period}))"

# The billing metric: where AWS publishes it, its namespace, name and dimension, and how often.
BILLING_REGION = "us-east-1"
BILLING = "AWS/Billing"
CHARGES = "EstimatedCharges"
CURRENCY: cloudwatch.Dimensions = {"Currency": "USD"}
# An hour: one reading or none in most periods, so that a period's maximum is its reading.
BILLING_PERIOD = 3600
# The data source's own region: the endpoint's.
OWN_REGION = "default"

# The dashboard's range, the month so far, as Grafana's relative times; and the time zone it is
# taken in: AWS bills by the month in UTC.
THIS_MONTH = "now/M"
NOW = "now"
TIME_ZONE = "utc"
# Of the count's hourly points in that month, today's: those of the range's last UTC day of the month.
TODAY = "IF(DATE({count}) == MAX(DATE({count})), {count})"
# The companion: the range it opens on, the hours of a class session and some.
SESSION_UID = "cost-session"
SESSION_TITLE = "Cost per session"
SESSION_DEFAULT = "now-3h"
# The panel's window, as Grafana gives it to a Prometheus query, its start in whole seconds (the
# window counted from there: no minute's step falls in the second it is cut from), the step
# the tokens are counted at (a minute, a few scrapes, so that a month is some 45,000 steps), and how
# far back a series' latest sample is read: Prometheus's own lookback.
WINDOW = rules.SESSION_RANGE
WINDOW_START = "${__from:date:seconds}"
TOKEN_STEP_SECONDS = 60
TOKEN_STEP = f"{TOKEN_STEP_SECONDS}s"
LOOKBACK = "5m"

# Every number is one colour, with no threshold: nothing is a ceiling.
COLOR = "blue"
USD = "currencyUSD"
COUNT = "none"
FIFTH = 4
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
    """One count of the pool's group, read by the group's name exactly, in the endpoint's region: its
    instance-minutes in each period."""
    query = metric(AUTO_SCALING, name, {GROUP_DIMENSION: group_name(pool.name)}, ref_id)
    return query.statistic("Sum").region(OWN_REGION).period(str(PERIOD))


def today(pool: Pool, ref_id: str) -> cloudwatch_builders.MetricsQuery:
    """Both counts of the pool's group, added, of today: one query of CloudWatch's metric math that
    refers to no other, in the endpoint's region, hidden."""
    metrics = " OR ".join(f'MetricName="{name}"' for name in INSTANCE_COUNTS)
    running = RUNNING.format(
        namespace=AUTO_SCALING,
        dimension=GROUP_DIMENSION,
        group=group_name(pool.name),
        metrics=metrics,
        period=PERIOD,
    )
    return (
        cloudwatch_builders.MetricsQuery()
        .query_mode(cloudwatch.QueryMode.METRICS)
        .metric_query_type(cloudwatch.MetricQueryType.SEARCH)
        .metric_editor_mode(cloudwatch.MetricEditorMode.CODE)
        .expression(TODAY.format(count=running))
        .id(ref_id.lower())
        .ref_id(ref_id)
        .hide(True)
        .region(OWN_REGION)
        .period(str(PERIOD))
        .datasource(rules.CLOUDWATCH)
    )


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


def hours(pool: Pool, suffix: str = "", *, of_today: bool = False) -> tuple[list[Target], str]:
    """The queries of the pool's counts, each summed over the window (today's points if `of_today`),
    and the server-side expression of the hours they add up to. `suffix` tells one pool's queries
    from another's."""
    if of_today:
        return [today(pool, f"D{suffix}"), reduce(f"S{suffix}", f"D{suffix}", "sum")], f"$S{suffix} / {MINUTES_AN_HOUR}"
    targets: list[Target] = []
    summed = []
    for name, letter in zip(INSTANCE_COUNTS, ("N", "W"), strict=True):
        targets.append(count(pool, name, f"{letter}{suffix}"))
        targets.append(reduce(f"S{letter}{suffix}", f"{letter}{suffix}", "sum"))
        summed.append(f"$S{letter}{suffix}")
    return targets, f"({' + '.join(summed)}) / {MINUTES_AN_HOUR}"


def dollars(pool: Pool, suffix: str = "", *, of_today: bool = False) -> tuple[list[Target], str]:
    """The same queries, and the expression of those hours at the pool's price, in dollars."""
    targets, expression = hours(pool, suffix, of_today=of_today)
    return targets, f"{expression} * {price(pool)!r}"


def settled(now: str, before: str) -> str:
    """The tokens each series settled since `before`: what it rose by, or all of its value where it
    was not there then or fell (it was created again, from zero)."""
    return f"((({now} - {before}) >= 0) or {now})"


def output_tokens(pool: Pool) -> str:
    """The output tokens the gateway settled for the pool over the panel's window, read at its end:
    summed over the minutes of the window after its start, each step counted from the series' latest
    sample a minute before, or at the window's start for the first, and from its last minute to its
    end; none where that is no token."""
    series = rules.selector(OUTPUT_TOKENS + rules.COUNTER_SAMPLE, {rules.POOL: pool.name})
    latest = f"last_over_time({series}[{LOOKBACK}])"
    first = f"vector(time()) - {TOKEN_STEP_SECONDS} < {WINDOW_START}"
    a_step_before = f"last_over_time({series}[{LOOKBACK}] offset {TOKEN_STEP})"
    at_start = f"last_over_time({series}[{LOOKBACK}] @ {WINDOW_START})"
    # Per series: its latest a step before, unless that is before the start; then its latest at the start.
    before = f"(({a_step_before} unless on () ({first})) or ({at_start} and on () ({first})))"
    after_start = f"vector(time()) > {WINDOW_START}"
    steps = f"sum_over_time((sum({settled(latest, before)}) and on () ({after_start}))[{WINDOW}:{TOKEN_STEP}])"
    since_last_step = f"sum({settled(latest, f'last_over_time({latest}[{TOKEN_STEP}:{TOKEN_STEP}])')})"
    total = " + ".join(f"(({one}) or vector(0))" for one in (steps, since_last_step))
    return f"({total}) > 0"


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


# Each calendar window: its name in a title, and since when, in a description.
_CALENDAR = {
    True: ("today", "from the start of the day, UTC"),
    False: ("this month", "from the start of the month, UTC"),
}


def hours_stat(pool: Pool, *, of_today: bool) -> stat_builders.Panel:
    """The pool's GPU-hours of the day, or of the month."""
    name, since = _CALENDAR[of_today]
    description = (
        f"The hours of the pool's running instances, warm-pool preparation included, {since} to now: "
        f"Auto Scaling's {' plus '.join(INSTANCE_COUNTS)} of its group."
    )
    targets, expression = hours(pool, of_today=of_today)
    panel = _stat(f"GPU-hours {name}, estimate", description, COUNT).datasource(rules.CLOUDWATCH)
    return _with(panel, targets).with_target(math("H", expression))


def dollars_stat(pool: Pool, *, of_today: bool) -> stat_builders.Panel:
    """The pool's estimated cost of the day, or of the month."""
    name, since = _CALENDAR[of_today]
    description = (
        f"The GPU-hours {since} to now, at ${price(pool)!r} an hour, the configured price of {pool.instance_types[0]}."
    )
    targets, expression = dollars(pool, of_today=of_today)
    panel = _stat(f"Cost {name}, estimate", description, USD).datasource(rules.CLOUDWATCH)
    return _with(panel, targets).with_target(math("E", expression))


def per_million_stat(pool: Pool, *, of_session: bool) -> stat_builders.Panel:
    """The pool's estimated cost per million output tokens over the dashboard's time range: the
    month so far on the dashboard, the session chosen on its companion."""
    if of_session:
        title, over = "Cost per million output tokens, dashboard's range, estimate", "over the dashboard's time range"
    else:
        title, over = "Cost per million output tokens, this month, estimate", "from the start of the month, UTC, to now"
    description = (
        f"The estimated cost of the pool's instances, at ${price(pool)!r} an hour, over the output tokens "
        f"the gateway settled for the pool, in millions, {over}."
    )
    targets, expression = dollars(pool)
    panel = _with(_stat(title, description, USD).datasource(rules.MIXED), targets)
    panel.with_target(rules.query(output_tokens(pool), "tokens", "B", instant=True).hide(True))
    panel.with_target(reduce("D", "B", "last"))
    return panel.with_target(math("E", f"{expression} / ($D / 1000000)"))


def bill_stat(config: Config) -> stat_builders.Panel:
    """The account's month-to-date charge, beside the month's estimate over every pool."""
    description = (
        "The account's charge since the month began, from CloudWatch's billing metric in US East "
        "(N. Virginia), beside the estimated cost of every pool's instances over the same month, "
        "in UTC. The bill also holds the always-on platform, storage and traffic."
    )
    panel = (
        _stat("The bill beside the estimate, month to date", description, USD)
        .datasource(rules.CLOUDWATCH)
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


def calendar(config: Config) -> dashboard_builders.Dashboard:
    """The dashboard, over the month so far, with no time picker: one row for each pool of the
    configuration, in its order, then the bill."""
    built = (
        dashboard_builders.Dashboard(TITLE)
        .uid(UID)
        .timezone(TIME_ZONE)
        .time(THIS_MONTH, NOW)
        .timepicker(dashboard_builders.TimePicker().hidden(True))
    )
    for pool in config.pools:
        built.with_row(dashboard_builders.Row(pool.name))
        for panel in (
            hours_stat(pool, of_today=True),
            hours_stat(pool, of_today=False),
            dollars_stat(pool, of_today=True),
            dollars_stat(pool, of_today=False),
        ):
            built.with_panel(panel.span(FIFTH))
        built.with_panel(per_million_stat(pool, of_session=False).span(2 * FIFTH))
    built.with_row(dashboard_builders.Row(BILLING_ROW))
    built.with_panel(bill_stat(config).span(HALF))
    return built


def session(config: Config) -> dashboard_builders.Dashboard:
    """The companion: the cost per million output tokens of each pool over the range chosen."""
    built = dashboard_builders.Dashboard(SESSION_TITLE).uid(SESSION_UID).timezone(TIME_ZONE).time(SESSION_DEFAULT, NOW)
    for pool in config.pools:
        built.with_row(dashboard_builders.Row(pool.name))
        built.with_panel(per_million_stat(pool, of_session=True).span(HALF))
    return built


def dashboard(config: Config) -> tuple[dashboard_builders.Dashboard, dashboard_builders.Dashboard]:
    """The dashboard, and its companion over a session."""
    return calendar(config), session(config)
