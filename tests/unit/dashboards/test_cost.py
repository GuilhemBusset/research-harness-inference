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
# Today's count: the group's total instances, of the group of that name exactly, and no other metric.
SEARCH = re.compile(
    r"SUM\(SEARCH\('\{AWS/AutoScaling,AutoScalingGroupName\} "
    r'AutoScalingGroupName="(?P<group>[^"]+)" MetricName="GroupTotalInstances"\', \'Sum\', 3600\)\)'
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
    return [
        one
        for one in of_type(panel, "cloudwatch")
        if one.get("namespace") == "AWS/AutoScaling" or "AWS/AutoScaling" in one.get("expression", "")
    ]


def searched(expression: str) -> re.Match[str]:
    """The search of today's count in a CloudWatch expression."""
    match = SEARCH.search(expression)
    assert match, expression
    return match


def groups_read(panel: dict[str, Any]) -> set[str]:
    """The names of the groups whose counts the panel reads."""
    found = set()
    for query in counts(panel):
        if "dimensions" in query:
            found.add(query["dimensions"]["AutoScalingGroupName"])
        else:
            found.update(match["group"] for match in SEARCH.finditer(query["expression"]))
    return found


def read_exactly(panel: dict[str, Any], suffix: str = "") -> dict[str, str]:
    """The group the month's count is read from, by the count's name; it is read whole."""
    reads = {one["metricName"]: one for one in counts(panel) if one["refId"].endswith(suffix)}
    for one in reads.values():
        assert (one["matchExact"], one["statistic"], one["period"], one["region"]) == (True, "Sum", "3600", "default")
        assert "expression" not in one and list(one["dimensions"]) == ["AutoScalingGroupName"]
    return {name: one["dimensions"]["AutoScalingGroupName"] for name, one in reads.items()}


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


def test_the_gpu_hours_read_the_total_instances_of_each_pools_group_and_no_other_count(example: rhi_config.Config) -> None:
    for pool in example.pools:
        group = f"rhi-pool-{pool.name}"
        for panel in rows(example)[pool.name][:4]:
            assert groups_read(panel) == {group}
            if "today" in panel["title"]:
                (day,) = counts(panel)
                assert searched(day["expression"])["group"] == group
                assert re.findall(r'MetricName="([^"]+)"', day["expression"]) == ["GroupTotalInstances"] * 3
            else:
                assert read_exactly(panel) == {"GroupTotalInstances": group}
        hours = [panel for panel in rows(example)[pool.name] if panel["title"].startswith("GPU-hours")]
        assert [panel["title"] for panel in hours] == ["GPU-hours today, estimate", "GPU-hours this month, estimate"]
        assert [shown(panel) for panel in hours] == [{"H": "$S / 60"}, {"H": "$S / 60"}]


def test_pools_of_similar_names_read_only_their_own_groups(example: rhi_config.Config) -> None:
    mock = next(pool for pool in example.pools if pool.name == "mock")
    similar = dataclasses.replace(example, pools=(mock, dataclasses.replace(mock, name="mo-ck")))
    for pool in similar.pools:
        assert all(groups_read(panel) == {f"rhi-pool-{pool.name}"} for panel in rows(similar)[pool.name])
    (bill,) = rows(similar)[cost.BILLING_ROW]
    assert [read_exactly(bill, suffix) for suffix in "01"] == [
        {cost.INSTANCE_COUNT: "rhi-pool-mock"},
        {cost.INSTANCE_COUNT: "rhi-pool-mo-ck"},
    ]


def test_the_months_hours_never_rest_on_a_search(example: rhi_config.Config) -> None:
    # A search finds only the metrics that reported in the past two weeks: a group deleted earlier in
    # the month, a parked stage's, would leave the month's estimate and the bill's comparison.
    monthly = [panel for panel in every_panel(example) if counts(panel) and "today" not in panel["title"]]
    assert len(monthly) == 3 * len(example.pools) + 1 + len(example.pools)
    for panel in monthly:
        assert all("SEARCH" not in one.get("expression", "") for one in counts(panel))
        assert {one["metricName"] for one in counts(panel)} == {"GroupTotalInstances"}


def test_a_count_with_no_point_adds_nothing_to_another_pools(example: rhi_config.Config) -> None:
    # Grafana gives no number for a sum one of whose terms is no data: each pool's count is reduced on
    # its own, and a read whole gives a series, with no point for a group that published none.
    for panel in every_panel(example):
        reduced = {one["refId"]: one["expression"] for one in panel["targets"] if one.get("type") == "reduce"}
        for read in counts(panel):
            assert sum(expression == f"${read['refId']}" for expression in reduced.values()) == 1


def test_each_cloudwatch_query_with_server_side_expressions_refers_to_no_other(example: rhi_config.Config) -> None:
    # Grafana sends each query of a panel with server-side expressions to its data source on its own.
    for panel in every_panel(example):
        assert any(target["datasource"]["type"] == "__expr__" for target in panel["targets"])
        queries = of_type(panel, "cloudwatch")
        ids = {one["id"] for one in queries}
        for one in queries:
            words = set(re.findall(r"\b[a-z]\w*\b", re.sub(r"'[^']*'", "", one.get("expression", ""))))
            assert not words & (ids - {one["id"]}), (panel["title"], one["expression"])


def test_today_is_the_last_utc_day_of_the_months_hourly_points(example: rhi_config.Config) -> None:
    for pool in example.pools:
        for panel in rows(example)[pool.name][:4]:
            if "today" not in panel["title"]:
                continue
            # The day: CloudWatch's metric math over the count, of one query, keeps its points of the
            # range's last UTC day of the month.
            by_ref = {one["refId"]: one for one in panel["targets"]}
            assert by_ref["S"]["expression"] == "$D"
            day = by_ref["D"]
            count = searched(day["expression"])[0]
            assert day["expression"] == f"IF(DATE({count}) == MAX(DATE({count})), {count})"
            # Grafana's CloudWatch backend refuses a query with no statistic, metric math included.
            assert (day["id"], day["hide"], day["region"], day["period"], day["metricEditorMode"], day["statistic"]) == (
                "d",
                True,
                "default",
                "3600",
                1,
                "Sum",
            )


def test_the_price_is_the_example_configurations(example: rhi_config.Config) -> None:
    assert {pool.name: example_price(pool.name) for pool in example.pools} == {"coursework": 3.36, "mock": 0.02}
    for pool in example.pools:
        for panel in rows(example)[pool.name]:
            if panel["title"].startswith("Cost"):
                (expression,) = shown(panel).values()
                assert f" / 60 * {example_price(pool.name)!r}" in expression
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
    # Every minute of the window after its start, a step from the series' latest sample a minute
    # before, or, at the first minute, at the window's start: nothing settled before it is counted,
    # and a restart in that minute keeps what the series settled after it.
    latest = f"last_over_time({series}[5m])"
    first = "vector(time()) - 60 < ${__from:date:seconds}"
    before = (
        f"((last_over_time({series}[5m] offset 60s) unless on () ({first}))"
        f" or (last_over_time({series}[5m] @ ${{__from:date:seconds}}) and on () ({first})))"
    )
    step = f"sum(((({latest} - {before}) >= 0) or {latest}))"
    after_start = "and on () (vector(time()) > ${__from:date:seconds})"
    assert f"sum_over_time(({step} {after_start})[${{__range_ms}}ms:60s])" in expression
    # From the last minute's step to the window's end, or from its start where no minute falls in
    # it (a window from 00:30 to 00:50 counts nothing from 00:00); nothing is taken off the steps.
    no_step = "vector(time() - time() % 60) < ${__from:date:seconds}"
    last_before = (
        f"((last_over_time({latest}[60s:60s]) unless on () ({no_step}))"
        f" or (last_over_time({series}[5m] @ ${{__from:date:seconds}}) and on () ({no_step})))"
    )
    since = f"(sum(((({latest} - {last_before}) >= 0) or {latest}))) or vector(0)"
    assert expression.endswith(f" + ({since})) > 0")
    assert not re.search(r"-\s*\(+sum\(", expression)


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
    assert groups_read(panel) == {f"rhi-pool-{pool.name}" for pool in example.pools}
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
