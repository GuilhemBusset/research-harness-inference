"""The cost dashboard, built from the example configuration (system design, sections 4.7, 7.2, 7.3 and 11.3)."""

from __future__ import annotations

import dataclasses
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from rhi import config as rhi_config
from rhi_dashboards import build, check, cost

REPO = Path(__file__).resolve().parents[3]
# A pool's count: both of its group's metrics, added in CloudWatch.
SEARCH = re.compile(
    r"SUM\(SEARCH\('\{AWS/AutoScaling,AutoScalingGroupName\} "
    r'\(MetricName="GroupTotalInstances" OR MetricName="WarmPoolPendingCapacity"\) '
    r"AutoScalingGroupName=(?P<group>\w+)', 'Sum', 3600\)\)"
)


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


def document(config: rhi_config.Config, uid: str = cost.UID) -> dict[str, Any]:
    """The dashboard, or its companion over a session, as the build writes it."""
    (built,) = [one for one in build.dashboards(config) if one.uid == uid]
    assert built.module == cost.__name__
    return built.document()


def rows(config: rhi_config.Config, uid: str = cost.UID) -> dict[str, list[dict[str, Any]]]:
    """The dashboard's panels, under the title of the row each follows."""
    found: dict[str, list[dict[str, Any]]] = {}
    for panel in document(config, uid)["panels"]:
        if panel["type"] == "row":
            found[panel["title"]] = []
        else:
            found[list(found)[-1]].append(panel)
    return found


def every_panel(config: rhi_config.Config) -> list[dict[str, Any]]:
    """Every panel of the dashboard and of its companion."""
    return [panel for uid in (cost.UID, cost.SESSION_UID) for row in rows(config, uid).values() for panel in row]


def of_type(panel: dict[str, Any], source: str) -> list[dict[str, Any]]:
    return [target for target in panel["targets"] if target["datasource"]["type"] == source]


def counts(panel: dict[str, Any]) -> list[dict[str, Any]]:
    """The panel's queries of Auto Scaling's group metrics."""
    return [one for one in of_type(panel, "cloudwatch") if "AWS/AutoScaling" in one.get("expression", "")]


def searched_group(query: dict[str, Any]) -> str:
    """How the names of the groups a count adds begin: the pool's group's logical ID."""
    match = SEARCH.fullmatch(query["expression"])
    assert match, query["expression"]
    return match["group"]


def shown(panel: dict[str, Any]) -> dict[str, str]:
    """The expression of each query the panel shows, by its reference."""
    return {one["refId"]: one.get("expression", "") for one in panel["targets"] if not one.get("hide")}


def example_price(pool: str) -> float:
    """The hourly price `config.example.toml` gives the pool's first instance type."""
    entry = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))["pools"][pool]
    return entry["prices"][entry["instance_types"][0]]


def test_each_pool_has_its_row_and_the_bill_its_own(example: rhi_config.Config) -> None:
    assert list(rows(example)) == [*(pool.name for pool in example.pools), cost.BILLING_ROW]
    assert all(len(rows(example)[pool.name]) == 5 for pool in example.pools)
    assert {pool: len(panels) for pool, panels in rows(example, cost.SESSION_UID).items()} == {
        pool.name: 1 for pool in example.pools
    }


def test_every_panel_from_the_instance_count_says_estimate(example: rhi_config.Config) -> None:
    from_the_count = [panel for panel in every_panel(example) if counts(panel)]
    # Every panel of a pool's row, the bill's, which holds the month's estimate, and the session's.
    assert len(from_the_count) == 5 * len(example.pools) + 1 + len(example.pools)
    assert all("estimate" in panel["title"].lower() for panel in from_the_count)


def test_a_fixed_session_never_replaces_the_calendar_windows(example: rhi_config.Config) -> None:
    # Grafana gives every panel a dashboard's fixed dates, whatever window a panel asks for: no
    # panel asks for one. The calendar panels read the dashboard's range, the month so far, which
    # has no time picker; a session is chosen on the companion, which has no calendar panel.
    calendar, companion = document(example), document(example, cost.SESSION_UID)
    assert all("timeFrom" not in panel and "timeShift" not in panel for panel in every_panel(example))
    assert (calendar["time"], calendar["timepicker"]["hidden"]) == ({"from": "now/M", "to": "now"}, True)
    assert not companion.get("timepicker", {}).get("hidden")
    titles = [panel["title"] for row in rows(example, cost.SESSION_UID).values() for panel in row]
    assert titles and not [title for title in titles if re.search(r"today|month", title)]


def test_the_gpu_hours_add_both_counts_of_each_pools_group_in_cloudwatch(example: rhi_config.Config) -> None:
    for pool in example.pools:
        for panel in rows(example)[pool.name]:
            (read,) = counts(panel)
            # One series: both counts of every group whose CloudFormation name holds the pool's
            # logical ID, a group replaced included, summed over each hour, in the endpoint's region.
            assert searched_group(read) == f"Pool{pool.name}Group"
            assert (read["refId"], read["id"], read["hide"], read["region"]) == ("N", "n", True, "default")
            by_ref = {one["refId"]: one for one in panel["targets"]}
            assert by_ref["S"]["reducer"] == "sum"
        hours = [panel for panel in rows(example)[pool.name] if panel["title"].startswith("GPU-hours")]
        assert [panel["title"] for panel in hours] == ["GPU-hours today, estimate", "GPU-hours this month, estimate"]
        assert all(shown(panel) == {"H": "$S / 60"} for panel in hours)


def test_a_group_with_no_warm_pool_series_still_counts_its_instances(example: rhi_config.Config) -> None:
    # Grafana gives no number for a sum one of whose terms has no series, as the warm pool's
    # count of a group without one: CloudWatch adds the two counts, so Grafana reduces one series.
    pool = dataclasses.replace(example.pools[0], warm_instances=0)
    for panel in rows(dataclasses.replace(example, pools=(pool,)))[pool.name]:
        reduced = [one["expression"] for one in panel["targets"] if one.get("type") == "reduce"]
        assert reduced in (["$N"], ["$DN"], ["$N", "$B"])
        assert all(target.get("metricName") is None for target in counts(panel))


def test_today_is_the_last_utc_day_of_the_months_hourly_points(example: rhi_config.Config) -> None:
    for pool in example.pools:
        for panel in rows(example)[pool.name][:4]:
            by_ref = {one["refId"]: one for one in panel["targets"]}
            if "today" not in panel["title"]:
                # The month: every point of the dashboard's range.
                assert by_ref["S"]["expression"] == "$N"
                continue
            # The day: CloudWatch's metric math over the count, in the same request, keeps its points
            # of the range's last UTC day of the month, a zero for an hour with none.
            assert by_ref["S"]["expression"] == "$DN"
            day = by_ref["DN"]
            assert (day["expression"], day["id"], day["hide"]) == ("IF(DATE(n) == MAX(DATE(n)), n)", "dn", True)
            assert (day["region"], day["period"], day["metricEditorMode"]) == ("default", "3600", 1)


def test_the_price_is_the_example_configurations(example: rhi_config.Config) -> None:
    assert {pool.name: example_price(pool.name) for pool in example.pools} == {"coursework": 3.36, "mock": 0.02}
    for pool in example.pools:
        for panel in rows(example)[pool.name]:
            if panel["title"].startswith("Cost"):
                (expression,) = shown(panel).values()
                assert f"$S / 60 * {example_price(pool.name)!r}" in expression
    (bill,) = rows(example)[cost.BILLING_ROW]
    assert shown(bill)["D"] == "($S0 / 60 * 3.36) + ($S1 / 60 * 0.02)"


def test_the_cost_per_million_is_the_dollars_over_the_settled_output_tokens_in_millions(
    example: rhi_config.Config,
) -> None:
    for pool in example.pools:
        # Over the companion's range, a session, and over the dashboard's, the month so far.
        per_million = [*rows(example, cost.SESSION_UID)[pool.name], rows(example)[pool.name][4]]
        assert [panel["title"] for panel in per_million] == [
            "Cost per million output tokens, dashboard's range, estimate",
            "Cost per million output tokens, this month, estimate",
        ]
        for panel in per_million:
            assert panel["datasource"] == {"type": "datasource", "uid": "-- Mixed --"}
            by_ref = {target["refId"]: target for target in panel["targets"]}
            assert shown(panel) == {"E": f"$S / 60 * {example_price(pool.name)!r} / ($D / 1000000)"}
            assert (by_ref["D"]["expression"], by_ref["D"]["reducer"]) == ("$B", "last")
            assert (by_ref["B"]["expr"], by_ref["B"]["instant"]) == (cost.output_tokens(pool), True)


def test_the_tokens_are_counted_from_the_windows_start_by_the_samples_taken_in_it(example: rhi_config.Config) -> None:
    pool = example.pools[0]
    expression = cost.output_tokens(pool)
    series = f'rhi_gateway_student_output_tokens_total{{pool="{pool.name}"}}'
    # A series is read by its latest sample over a range, never by Prometheus's instant value, which
    # a series removed between two minutes no longer has.
    selectors = re.findall(re.escape(series) + r"(\[[^]]+\])?", expression)
    assert selectors and all(window == "[5m]" for window in selectors)
    assert expression.count(f"last_over_time({series}[5m]") == len(selectors)
    # Every minute of the window after its start, a step from the series' latest sample a minute before.
    latest = f"last_over_time({series}[5m])"
    step = f"sum(((({latest} - last_over_time({series}[5m] offset 1m)) >= 0) or {latest}))"
    after_start = "and on () (vector(time()) > ${__from:date:seconds})"
    assert f"sum_over_time(({step} {after_start})[${{__range_ms}}ms:1m])" in expression
    # What the first minute counted from before the start is taken off: the series' rise from the
    # minute's step before the start to the start.
    at_start = f"last_over_time({series}[5m] @ ${{__from:date:seconds}})"
    before = f"last_over_time({latest}[1m:1m] @ ${{__from:date:seconds}})"
    assert f"- ((sum(((({at_start} - {before}) >= 0) or {at_start}))) or vector(0))" in expression
    # From the last minute's step to the window's end; no number where no token was settled.
    assert f"(sum(((({latest} - last_over_time({latest}[1m:1m])) >= 0) or {latest}))) or vector(0)" in expression
    assert expression.endswith(" > 0")


def test_no_panel_shows_a_ceiling(example: rhi_config.Config) -> None:
    for panel in every_panel(example):
        assert [step["value"] for step in panel["fieldConfig"]["defaults"]["thresholds"]["steps"]] == [None]
        words = f"{panel['title']} {panel['description']}".lower()
        assert not {"ceiling", "cap", "limit", "budget"} & set(re.findall(r"[a-z]+", words))


def test_exactly_one_panel_reads_the_billing_metric_in_us_east_1_beside_the_months_estimate(
    example: rhi_config.Config,
) -> None:
    billing = [
        (panel, target)
        for panel in every_panel(example)
        for target in of_type(panel, "cloudwatch")
        if target.get("namespace") == "AWS/Billing"
    ]
    ((panel, charge),) = billing
    assert (charge["metricName"], charge["dimensions"], charge["matchExact"]) == (
        "EstimatedCharges",
        {"Currency": "USD"},
        True,
    )
    assert charge["region"] == "us-east-1"
    # Over the dashboard's range, the month so far.
    assert panel in rows(example)[cost.BILLING_ROW]
    # Beside it, shown, the estimate of every pool's group over the same month.
    assert set(shown(panel)) == {"C", "D"} and shown(panel)["C"] == f"${charge['refId']}"
    # The latest reading of the charge, not its highest: AWS may lower its estimate of the month.
    by_ref = {target["refId"]: target for target in panel["targets"]}
    assert (charge["statistic"], charge["hide"]) == ("Maximum", True)
    assert (by_ref["C"]["reducer"], by_ref["C"]["settings"]["mode"]) == ("last", "dropNN")
    assert {searched_group(one) for one in counts(panel)} == {
        f"Pool{pool.name}Group" for pool in example.pools
    }
    # Every other CloudWatch query reads the endpoint's own region.
    others = [
        target
        for one in every_panel(example)
        for target in of_type(one, "cloudwatch")
        if target.get("namespace") != "AWS/Billing"
    ]
    assert others and all(target["region"] == "default" for target in others)


def test_the_dashboards_follow_the_rules_and_their_windows_are_in_utc(example: rhi_config.Config) -> None:
    built = [one for one in build.dashboards(example) if one.module == cost.__name__]
    assert [one.uid for one in built] == [cost.UID, cost.SESSION_UID]
    for one in built:
        assert check.violations(one.document(), one.metrics, example.pools) == []
        assert one.document()["timezone"] == "utc"
