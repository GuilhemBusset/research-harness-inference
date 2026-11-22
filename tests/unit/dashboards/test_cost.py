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

REPO = Path(__file__).resolve().parents[3]
# One SEARCH of Auto Scaling's group metrics: what it searches, then its statistic and period.
SEARCH = re.compile(r"SEARCH\('(?P<terms>[^']*)', '(?P<statistic>\w+)', (?P<period>\d+)\)")


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


def searches(panel: dict[str, Any]) -> list[re.Match[str]]:
    """Every SEARCH in the panel's CloudWatch metric math."""
    return [found for one in of_type(panel, "cloudwatch") for found in SEARCH.finditer(one.get("expression", ""))]


def example_price(pool: str) -> float:
    """The hourly price `config.example.toml` gives the pool's first instance type."""
    entry = tomllib.loads((REPO / "config.example.toml").read_text(encoding="utf-8"))["pools"][pool]
    return entry["prices"][entry["instance_types"][0]]


def test_each_pool_has_its_row_and_the_bill_its_own(example: rhi_config.Config) -> None:
    assert list(rows(example)) == [*(pool.name for pool in example.pools), cost.BILLING_ROW]
    assert all(len(rows(example)[pool.name]) == 6 for pool in example.pools)


def test_every_panel_from_the_instance_count_says_estimate(example: rhi_config.Config) -> None:
    from_the_count = [panel for panel in every_panel(example) if searches(panel)]
    # Every panel of a pool's row, and the bill's, which holds the month's estimate.
    assert len(from_the_count) == 6 * len(example.pools) + 1
    assert all("estimate" in panel["title"].lower() for panel in from_the_count)


def test_the_gpu_hours_read_both_counts_of_each_pools_group(example: rhi_config.Config) -> None:
    for pool in example.pools:
        others = [cost.group_logical_id(other) for other in example.pools if other != pool]
        for panel in rows(example)[pool.name]:
            for search in searches(panel):
                terms = search["terms"]
                assert terms.startswith("{AWS/AutoScaling,AutoScalingGroupName} ")
                assert '(MetricName="GroupTotalInstances" OR MetricName="WarmPoolPendingCapacity")' in terms
                assert terms.endswith(f" AutoScalingGroupName={cost.group_logical_id(pool)}")
                assert not any(other in terms for other in others)
                # Each one-minute point is the count in its minute: summed, then in hours.
                assert search["statistic"] == "Sum"
                assert f"{search[0]}) / 60" in panel["targets"][0]["expression"]
        hours = [panel for panel in rows(example)[pool.name] if panel["title"].startswith("GPU-hours")]
        # The day and the month so far, each summed over its hours.
        assert [panel["timeFrom"] for panel in hours] == ["now/d", "now/M"]
        assert all(panel["options"]["reduceOptions"]["calcs"] == ["sum"] for panel in hours)


def test_the_price_is_the_example_configurations(example: rhi_config.Config) -> None:
    assert {pool.name: example_price(pool.name) for pool in example.pools} == {"coursework": 3.36, "mock": 0.02}
    for pool in example.pools:
        for panel in rows(example)[pool.name]:
            if panel["title"].startswith("GPU-hours"):
                continue
            (dollars,) = of_type(panel, "cloudwatch")
            (shown,) = re.findall(r"\* ([0-9.]+)$", dollars["expression"])
            assert float(shown) == example_price(pool.name)
    (bill,) = rows(example)[cost.BILLING_ROW]
    (estimate,) = [one for one in of_type(bill, "cloudwatch") if "expression" in one]
    assert sorted(float(one) for one in re.findall(r"\* ([0-9.]+)\)", estimate["expression"])) == [0.02, 3.36]


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
            assert [ref for ref, target in by_ref.items() if not target.get("hide")] == ["E"]
            assert by_ref["E"]["expression"] == "$C / ($D / 1000000)"
            assert (by_ref["C"]["expression"], by_ref["C"]["reducer"]) == ("$A", "sum")
            assert by_ref["D"]["expression"] == "$B"
            assert by_ref["A"]["expression"] == cost.dollars(pool)
            tokens = rules.increase(f"{metrics.OUTPUT_TOKENS}_total", {"pool": pool.name}, window="$__range")
            assert (by_ref["B"]["expr"], by_ref["B"]["instant"]) == (tokens, True)


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
    by_ref = {target["refId"]: target for target in panel["targets"]}
    shown = {ref: target["expression"] for ref, target in by_ref.items() if not target.get("hide")}
    assert shown == {"C": f"${charge['refId']}", "D": "$B"}
    assert {search["terms"].rpartition("=")[2] for search in searches(panel)} == {
        cost.group_logical_id(pool) for pool in example.pools
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
