"""The students dashboard, built from the example configuration (system design, sections 6.6, 7.2 and 7.3)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from rhi import config as rhi_config
from rhi_dashboards import build, check, students
from rhi_gateway import metrics

# The six quantities, by the gateway's families.
QUANTITIES = (
    metrics.REQUESTS,
    metrics.INPUT_TOKENS,
    metrics.CACHED_INPUT_TOKENS,
    metrics.OUTPUT_TOKENS,
    metrics.COUNTED_TOKENS,
    metrics.ERRORS,
)
# What a query may keep: the pool, the cause of an error, and the alias, the one name of a student.
KEPT = {"pool", "alias", "cause"}


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


def built(config: rhi_config.Config) -> build.Built:
    (one,) = [one for one in build.dashboards(config) if one.module == students.__name__]
    return one


def targets(panel: dict[str, Any]) -> list[dict[str, Any]]:
    return list(panel.get("targets", ()))


def test_each_quantity_has_its_panel_and_the_gateway_serves_its_series(example: rhi_config.Config) -> None:
    assert students in build.modules()
    panels = [panel for panel in built(example).document()["panels"] if panel["type"] != "row"]
    queried = [
        {family for family in QUANTITIES if f"{family}_total{{" in target["expr"]}
        for panel in panels
        for target in targets(panel)
    ]
    assert sorted(len(one) for one in queried) == [1] * len(QUANTITIES)
    assert sorted(family for (family,) in queried) == sorted(QUANTITIES)
    assert len(panels) == len(QUANTITIES)
    # Each family is one the gateway's registry serves, the cached and counted tokens included.
    served = {family.name for family in metrics.Metrics().registry.collect()}
    assert set(QUANTITIES) <= served


def test_every_query_is_by_alias_summed_over_tasks_for_students_only(example: rhi_config.Config) -> None:
    for panel in built(example).document()["panels"]:
        for target in targets(panel):
            expression = target["expr"]
            (grouped,) = re.findall(r"sum by \(([^)]*)\)", expression)
            labels = {label.strip() for label in grouped.split(",")}
            # The alias and no other name of a student; the task (instance) is summed away.
            assert "alias" in labels
            assert labels <= KEPT
            assert ("cause" in labels) == (f"{metrics.ERRORS}_total" in expression)
            assert set(re.findall(r"\{\{(\w+)\}\}", target["legendFormat"])) <= labels
            # The rate of each series, of an alias that is not empty: a student's, not unattributed traffic.
            assert re.search(r'rate\(rhi_gateway_student_\w+_total\{alias=~"\.\+"\}\[', expression)


def test_the_dashboard_follows_the_rules(example: rhi_config.Config) -> None:
    one = built(example)
    assert check.violations(one.document(), one.metrics, example.pools) == []
