"""The cost dashboard, built from the example configuration (system design, sections 4.7, 7.2, 7.3 and 11.3)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from rhi import config as rhi_config
from rhi_dashboards import build, check, cost, rules
from rhi_gateway import metrics
from rhi_gateway.telemetry import RequestRecord

REPO = Path(__file__).resolve().parents[3]
COUNTS = {"GroupTotalInstances", "WarmPoolPendingCapacity"}


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


def rows(config: rhi_config.Config) -> dict[str, list[dict[str, Any]]]:
    """The dashboard's panels, under the title of the row each follows."""
    (built,) = [one for one in build.dashboards(config) if one.module == cost.__name__]
    found: dict[str, list[dict[str, Any]]] = {}
    for panel in built.document()["panels"]:
        if panel["type"] == "row":
            found[panel["title"]] = []
        else:
            found[list(found)[-1]].append(panel)
    return found


def every_panel(config: rhi_config.Config) -> list[dict[str, Any]]:
    return [panel for row in rows(config).values() for panel in row]


def of_type(panel: dict[str, Any], source: str) -> list[dict[str, Any]]:
    return [target for target in panel["targets"] if target["datasource"]["type"] == source]


def counts(panel: dict[str, Any]) -> list[dict[str, Any]]:
    """The panel's queries of Auto Scaling's group metrics."""
    return [one for one in of_type(panel, "cloudwatch") if one.get("namespace") == "AWS/AutoScaling"]


def shown(panel: dict[str, Any]) -> dict[str, str]:
    """The expression of each query the panel shows, by its reference."""
    return {one["refId"]: one.get("expression", "") for one in panel["targets"] if not one.get("hide")}


def example_price(pool: str) -> float:
    """The hourly price `config.example.toml` gives the pool's first instance type."""
    entry = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))["pools"][pool]
    return entry["prices"][entry["instance_types"][0]]


def test_each_pool_has_its_row_and_the_bill_its_own(example: rhi_config.Config) -> None:
    assert list(rows(example)) == [*(pool.name for pool in example.pools), cost.BILLING_ROW]
    assert all(len(rows(example)[pool.name]) == 6 for pool in example.pools)


def test_every_panel_from_the_instance_count_says_estimate(example: rhi_config.Config) -> None:
    from_the_count = [panel for panel in every_panel(example) if counts(panel)]
    # Every panel of a pool's row, and the bill's, which holds the month's estimate.
    assert len(from_the_count) == 6 * len(example.pools) + 1
    assert all("estimate" in panel["title"].lower() for panel in from_the_count)


def test_the_gpu_hours_read_both_counts_of_each_pools_group_by_its_name(example: rhi_config.Config) -> None:
    for pool in example.pools:
        for panel in rows(example)[pool.name]:
            read = counts(panel)
            assert {one["metricName"] for one in read} == COUNTS and len(read) == 2
            for one in read:
                # One metric, read exactly by the group's fixed name: CloudWatch answers with every
                # point it keeps in the window, a group deleted and created again included. No
                # search, which finds only what reported in the past two weeks.
                assert (one["dimensions"], one["matchExact"]) == ({"AutoScalingGroupName": f"rhi-pool-{pool.name}"}, True)
                assert "expression" not in one and "sqlExpression" not in one
                # Each one-minute point is the count in its minute: summed over the window.
                assert (one["statistic"], one["hide"]) == ("Sum", True)
            by_ref = {one["refId"]: one for one in panel["targets"]}
            assert {by_ref[ref]["expression"]: by_ref[ref]["reducer"] for ref in ("ST", "SW")} == {"$T": "sum", "$W": "sum"}
            assert {by_ref[ref]["metricName"] for ref in ("T", "W")} == COUNTS
        hours = [panel for panel in rows(example)[pool.name] if panel["title"].startswith("GPU-hours")]
        # The day and the month so far, in hours.
        assert [panel["timeFrom"] for panel in hours] == ["now/d", "now/M"]
        assert all(shown(panel) == {"H": "($ST + $SW) / 60"} for panel in hours)


def test_the_price_is_the_example_configurations(example: rhi_config.Config) -> None:
    assert {pool.name: example_price(pool.name) for pool in example.pools} == {"coursework": 3.36, "mock": 0.02}
    for pool in example.pools:
        for panel in rows(example)[pool.name]:
            if panel["title"].startswith("Cost"):
                (expression,) = shown(panel).values()
                assert f"($ST + $SW) / 60 * {example_price(pool.name)!r}" in expression
    (bill,) = rows(example)[cost.BILLING_ROW]
    assert shown(bill)["D"] == "(($ST0 + $SW0) / 60 * 3.36) + (($ST1 + $SW1) / 60 * 0.02)"


def test_the_cost_per_million_is_the_dollars_over_the_settled_output_tokens_in_millions(
    example: rhi_config.Config,
) -> None:
    for pool in example.pools:
        per_million = [panel for panel in rows(example)[pool.name] if "per million" in panel["title"]]
        # Over the dashboard's time range, then the month so far.
        assert [panel.get("timeFrom") for panel in per_million] == [None, "now/M"]
        for panel in per_million:
            assert panel["datasource"] == {"type": "datasource", "uid": "-- Mixed --"}
            by_ref = {target["refId"]: target for target in panel["targets"]}
            assert shown(panel) == {"E": f"($ST + $SW) / 60 * {example_price(pool.name)!r} / ($D / 1000000)"}
            assert (by_ref["D"]["expression"], by_ref["D"]["reducer"]) == ("$B", "last")
            assert (by_ref["B"]["expr"], by_ref["B"]["instant"]) == (cost.output_tokens(pool), True)


def test_the_tokens_count_every_settlement_of_a_series_created_or_created_again_in_the_window(
    example: rhi_config.Config,
) -> None:
    pool = example.pools[0].name
    # The gateway creates an alias's series on its first settlement, already holding its tokens, and
    # after the series is removed (a revoked key), creates it again from zero.
    gateway = metrics.Metrics()
    series, labels = f"{metrics.OUTPUT_TOKENS}_total", {"pool": pool, "alias": "student-example"}
    assert gateway.registry.get_sample_value(series, labels) is None
    gateway.count(RequestRecord(time="", wire_format=None, pool=pool, alias="student-example", output_tokens=100))
    assert gateway.registry.get_sample_value(series, labels) == 100
    # So at each step, a series counts what it rose by, or all of its value where it was not there
    # the step before or fell: 100, then 10 created again, is 110. Summed by pool, over every minute
    # of the window, then from its last minute to its end.
    selected = rules.selector(series, {"pool": pool})
    step = f"sum by (pool) ((({selected} - {selected} offset 1m) >= 0) or {selected})"
    last = f"sum by (pool) ((({selected} - last_over_time({selected}[1m:1m])) >= 0) or {selected})"
    steps = f"sum_over_time(({step})[${{__range_ms}}ms:1m])"
    assert cost.output_tokens(example.pools[0]) == f"(({steps}) + ({last})) or ({steps}) or ({last})"


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
    assert panel["timeFrom"] == "now/M"
    # Beside it, shown, the estimate of every pool's group over the same month.
    assert set(shown(panel)) == {"C", "D"} and shown(panel)["C"] == f"${charge['refId']}"
    # The latest reading of the charge, not its highest: AWS may lower its estimate of the month.
    by_ref = {target["refId"]: target for target in panel["targets"]}
    assert (charge["statistic"], charge["hide"]) == ("Maximum", True)
    assert (by_ref["C"]["reducer"], by_ref["C"]["settings"]["mode"]) == ("last", "dropNN")
    assert {one["dimensions"]["AutoScalingGroupName"] for one in counts(panel)} == {
        f"rhi-pool-{pool.name}" for pool in example.pools
    }
    # Every other CloudWatch query reads the endpoint's own region.
    others = [
        target
        for one in every_panel(example)
        for target in of_type(one, "cloudwatch")
        if target.get("namespace") != "AWS/Billing"
    ]
    assert others and all(target["region"] == "default" for target in others)


def test_the_dashboard_follows_the_rules_and_its_windows_are_in_utc(example: rhi_config.Config) -> None:
    (built,) = [one for one in build.dashboards(example) if one.module == cost.__name__]
    assert check.violations(built.document(), built.metrics, example.pools) == []
    assert (built.document()["timezone"], built.document()["time"]) == ("utc", {"from": "now/M", "to": "now"})
