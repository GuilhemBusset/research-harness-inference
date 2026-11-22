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
runs before it stops). A group without a warm pool publishes no warm-pool series, which then adds
nothing. Each series has one data point a minute, the count during that minute, so the `Sum`
statistic over a period is the instance-minutes inside it, and divided by 60 the instance-hours.
A period cut short by the window's edge, the hour that is still running included, holds only its
own minutes: nothing counts a partial hour as a whole one. The hours are summed over the window.

**A pool's group is found by its logical ID.** CloudFormation names the group, from the stack's
name, the group's logical ID and a random suffix, so its name is known only in the account. The
logical ID is the CDK's, from the construct path `rhi.pools.stack` gives the group
(`group_logical_id`), and a CloudWatch search on it, within the dimension `AutoScalingGroupName`,
finds that pool's group, and a replaced group of the same pool too, whose hours the pool also ran.

**The price is the hourly price the configuration gives the pool's first instance type**, the one
the group launches first (section 5.4): a fallback type launched in its place is counted at that
price too. The dollars are the hours times it.

**The tokens are the output tokens the gateway settled for the pool**
(`rhi_gateway_student_output_tokens_total`, `rhi_gateway.metrics`), the increase of each series
over the window, then summed (section 7.2, first rule): an instance's price is over what it
produced, its output (section 4.7). A series a task starts in the window has no sample before its
first, so the first settlement it counts is not in the increase: the tokens are a little under
what was settled, and the cost per million a little over. Grafana's server-side expressions divide
the dollars, from CloudWatch, by the tokens, from Prometheus, in millions.

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
(section 11.2), storage, traffic.

**No panel shows a ceiling**: there are no GPU-hour ceilings (section 11.3). Every panel has one
colour: its thresholds hold the base step alone, where Grafana's default would turn red at 80.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Literal

from grafana_foundation_sdk.builders import cloudwatch as cloudwatch_builders
from grafana_foundation_sdk.builders import common as common_builders
from grafana_foundation_sdk.builders import dashboard as dashboard_builders
from grafana_foundation_sdk.builders import expr as expr_builders
from grafana_foundation_sdk.builders import stat as stat_builders
from grafana_foundation_sdk.models import cloudwatch, common
from grafana_foundation_sdk.models import dashboard as models

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

# The billing metric: where AWS publishes it, its namespace, name and dimension, and how often.
BILLING_REGION = "us-east-1"
BILLING = "AWS/Billing"
CHARGES = "EstimatedCharges"
CURRENCY: cloudwatch.Dimensions = {"Currency": "USD"}
BILLING_PERIOD = 21600
# The data source's own region: the endpoint's.
OWN_REGION = "default"

# The construct path `rhi.pools.stack` gives a pool's group, and the CDK's rule for a logical ID.
GROUP_PATH = ("Pool-{pool}", "Group")
_NOT_ALPHANUMERIC = re.compile(r"[^A-Za-z0-9]")
_HASH_LENGTH = 8

# The calendar windows, as Grafana's relative times, from their start to now; and the time zone
# they are taken in: AWS bills by the month in UTC.
TODAY = "now/d"
THIS_MONTH = "now/M"
TIME_ZONE = "utc"

# Every number is one colour, with no threshold: nothing is a ceiling.
COLOR = "blue"
USD = "currencyUSD"
COUNT = "none"
SIXTH = 4
HALF = 12
BILLING_ROW = "The bill, month to date"


def group_logical_id(pool: Pool) -> str:
    """The logical ID of the pool's Auto Scaling group in the pools stack, by the CDK's rule: the
    path's parts without what is not a letter or a digit, then the first 8 hexadecimal digits of
    the MD5 of the path, in capitals."""
    path = [part.format(pool=pool.name) for part in GROUP_PATH]
    human = "".join(_NOT_ALPHANUMERIC.sub("", part) for part in path)
    digest = hashlib.md5("/".join(path).encode(), usedforsecurity=False).hexdigest()
    return human + digest[:_HASH_LENGTH].upper()


def price(pool: Pool) -> float:
    """Dollars an hour: the configured price of the pool's first instance type."""
    return pool.prices[pool.instance_types[0]]


def instance_minutes(pool: Pool) -> str:
    """CloudWatch's metric math for the minutes the pool's instances ran in each period: the sum, in
    every group found for the pool, of the two counts' one-minute data points."""
    names = " OR ".join(f'MetricName="{name}"' for name in INSTANCE_COUNTS)
    search = f"{{{AUTO_SCALING},{GROUP_DIMENSION}}} ({names}) {GROUP_DIMENSION}={group_logical_id(pool)}"
    return f"SUM(SEARCH('{search}', 'Sum', {PERIOD}))"


def gpu_hours(pool: Pool) -> str:
    """The hours the pool's instances ran in each period."""
    return f"{instance_minutes(pool)} / {MINUTES_AN_HOUR}"


def dollars(pool: Pool) -> str:
    """Those hours at the pool's price, in dollars."""
    return f"{gpu_hours(pool)} * {price(pool)!r}"


def output_tokens(pool: Pool) -> str:
    """The output tokens the gateway settled for the pool over the panel's window."""
    return rules.increase(OUTPUT_TOKENS + rules.COUNTER_SAMPLE, {rules.POOL: pool.name}, window="$__range")


def cloudwatch_math(expression: str, ref_id: str, *, hide: bool = False) -> cloudwatch_builders.MetricsQuery:
    """A query of the CloudWatch data source, in its own region: metric math, in code."""
    return (
        cloudwatch_builders.MetricsQuery()
        .query_mode(cloudwatch.QueryMode.METRICS)
        .metric_query_type(cloudwatch.MetricQueryType.SEARCH)
        .metric_editor_mode(cloudwatch.MetricEditorMode.CODE)
        .expression(expression)
        .id(ref_id.lower())
        .ref_id(ref_id)
        .region(OWN_REGION)
        .period(str(PERIOD))
        .hide(hide)
        .datasource(rules.CLOUDWATCH)
    )


def charges(ref_id: str) -> cloudwatch_builders.MetricsQuery:
    """The account's charge since the month began, in the one region AWS publishes it in."""
    return (
        cloudwatch_builders.MetricsQuery()
        .query_mode(cloudwatch.QueryMode.METRICS)
        .metric_query_type(cloudwatch.MetricQueryType.SEARCH)
        .metric_editor_mode(cloudwatch.MetricEditorMode.BUILDER)
        .namespace(BILLING)
        .metric_name(CHARGES)
        .dimensions(CURRENCY)
        .match_exact(True)
        .statistic("Maximum")
        .id(ref_id.lower())
        .ref_id(ref_id)
        .region(BILLING_REGION)
        .period(str(BILLING_PERIOD))
        .hide(True)
        .datasource(rules.CLOUDWATCH)
    )


# The reductions a server-side expression is asked for.
type Reducer = Literal["sum", "max", "last"]


def reduce(ref_id: str, of: str, reducer: Reducer, *, hide: bool = True) -> expr_builders.TypeReduce:
    """A server-side expression: the query `of` reduced to one number, shown unless `hide`."""
    return (
        expr_builders.TypeReduce()
        .expression(f"${of}")
        .reducer(reducer)
        .ref_id(ref_id)
        .hide(hide)
        .datasource(rules.EXPRESSIONS)
    )


def _one_colour() -> dashboard_builders.ThresholdsConfig:
    """One step, the base: no value is ever shown against a threshold."""
    steps = [models.Threshold(value=None, color=COLOR)]
    return dashboard_builders.ThresholdsConfig().mode(models.ThresholdsMode.ABSOLUTE).steps(steps)


def _stat(title: str, description: str, unit: str, calculation: str) -> stat_builders.Panel:
    return (
        stat_builders.Panel()
        .title(title)
        .description(description)
        .unit(unit)
        .decimals(2 if unit == USD else 1)
        .thresholds(_one_colour())
        .color_mode(common.BigValueColorMode.NONE)
        .graph_mode(common.BigValueGraphMode.NONE)
        .reduce_options(common_builders.ReduceDataOptions().calcs([calculation]))
    )


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
    panel = _stat(f"GPU-hours {name}, estimate", description, COUNT, "sum").datasource(rules.CLOUDWATCH)
    return panel.time_from(window).with_target(cloudwatch_math(gpu_hours(pool), "A"))


def dollars_stat(pool: Pool, window: str) -> stat_builders.Panel:
    """The pool's estimated cost over a calendar window."""
    name, since = _CALENDAR[window]
    description = (
        f"The GPU-hours {since} to now, at ${price(pool)!r} an hour, the configured price of "
        f"{pool.instance_types[0]}. {_FIXED_RANGE}"
    )
    panel = _stat(f"Cost {name}, estimate", description, USD, "sum").datasource(rules.CLOUDWATCH)
    return panel.time_from(window).with_target(cloudwatch_math(dollars(pool), "A"))


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
    panel = (
        _stat(title, description, USD, "lastNotNull")
        .datasource(rules.MIXED)
        .with_target(cloudwatch_math(dollars(pool), "A", hide=True))
        .with_target(rules.query(output_tokens(pool), "tokens", "B", instant=True).hide(True))
        .with_target(reduce("C", "A", "sum"))
        .with_target(reduce("D", "B", "last"))
        .with_target(
            expr_builders.TypeMath()
            .expression("$C / ($D / 1000000)")
            .ref_id("E")
            .datasource(rules.EXPRESSIONS)
        )
    )
    return panel if window is None else panel.time_from(window)


def bill_stat(config: Config) -> stat_builders.Panel:
    """The account's month-to-date charge, beside the month's estimate over every pool."""
    estimate = " + ".join(f"({dollars(pool)})" for pool in config.pools)
    description = (
        "The account's charge since the month began, from CloudWatch's billing metric in US East "
        "(N. Virginia), beside the estimated cost of every pool's instances over the same month, "
        f"in UTC. The bill also holds the always-on platform, storage and traffic. {_FIXED_RANGE}"
    )
    shown = {"C": "AWS charge, month to date", "D": "Instances, month to date, estimate"}
    panel = (
        _stat("The bill beside the estimate, month to date", description, USD, "lastNotNull")
        .datasource(rules.CLOUDWATCH)
        .time_from(THIS_MONTH)
        .with_target(charges("A"))
    )
    # With no pool, there is no instance to estimate.
    if estimate:
        panel.with_target(cloudwatch_math(estimate, "B", hide=True))
    panel.with_target(reduce("C", "A", "max", hide=False))
    if estimate:
        panel.with_target(reduce("D", "B", "sum", hide=False))
    for ref_id, name in shown.items():
        panel.override_by_query(ref_id, [models.DynamicConfigValue(id_val="displayName", value=name)])
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
