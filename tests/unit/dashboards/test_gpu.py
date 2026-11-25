"""The GPU health dashboard, built from the example configuration (system design, sections 7.2 and 7.3)."""

from __future__ import annotations

import csv
import dataclasses
import re
from typing import Any

import pytest
import yaml

from rhi import config as rhi_config
from rhi_common.engine_metrics import MOCK
from rhi_dashboards import build, check, gpu, rules

COUNTERS = check.REPO_ROOT / "services/engine/dcgm/counters.csv"
COLLECTOR = check.REPO_ROOT / "services/engine/collector/config.yaml"
GAUGES = (
    "DCGM_FI_DEV_MEMORY_UTIL_RATIO",
    "DCGM_FI_DEV_GPU_UTIL_RATIO",
    "DCGM_FI_DEV_FB_USED_BYTES",
    "DCGM_FI_DEV_GPU_TEMP_CELSIUS",
    "DCGM_FI_DEV_BOARD_POWER_WATTS",
)
ENERGY = "DCGM_FI_DEV_GPU_ENERGY_JOULES_TOTAL"
XID = "DCGM_FI_DEV_XID_ERROR"
SEVEN = (*GAUGES, ENERGY, XID)


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


def built(config: rhi_config.Config) -> build.Built:
    (one,) = [one for one in build.dashboards(config) if one.module == gpu.__name__]
    return one


def rows(config: rhi_config.Config) -> dict[str, list[dict[str, Any]]]:
    """The dashboard's panels, under the title of the row each follows."""
    found: dict[str, list[dict[str, Any]]] = {}
    for panel in built(config).document()["panels"]:
        if panel["type"] == "row":
            found[panel["title"]] = []
        else:
            found[list(found)[-1]].append(panel)
    return found


def queries(panel: dict[str, Any]) -> list[str]:
    return [target["expr"] for target in panel["targets"]]


def counter_types() -> dict[str, str]:
    """Each field of the exporter's counters file, with its Prometheus type: comments skipped."""
    lines = [line for line in COUNTERS.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
    return {row[0].strip(): row[1].strip() for row in csv.reader(lines) if row}


def published() -> dict[str, str]:
    """Each of the seven fields, by the series name the node's collector remote-writes it under.

    With its default suffixes, the collector's remote write gives a counter Prometheus' `_total`,
    and it counts only a lowercase one as already there. A gauge keeps its name.
    """
    exporter = yaml.safe_load(COLLECTOR.read_text(encoding="utf-8"))["exporters"]["prometheus_remote_write"]
    assert "translation_strategy" not in exporter
    assert exporter.get("add_metric_suffixes", True)
    types = counter_types()
    return {name: name.removesuffix("_total") + "_total" if types[name] == "counter" else name for name in SEVEN}


def two_gpu_pools(example: rhi_config.Config) -> rhi_config.Config:
    """The example configuration with a second GPU pool, a copy of its first, after the others."""
    first = next(pool for pool in example.pools if pool.engine != MOCK)
    return dataclasses.replace(example, pools=(*example.pools, dataclasses.replace(first, name="second")))


def test_every_gpu_pool_has_a_row_and_the_mock_pool_none(example: rhi_config.Config) -> None:
    engines = {pool.name: pool.engine for pool in example.pools}
    assert MOCK in engines.values()
    assert list(rows(example)) == [name for name, engine in engines.items() if engine != MOCK] == ["coursework"]

    first, second = (pool for pool in two_gpu_pools(example).pools if pool.engine != MOCK)
    two = rows(two_gpu_pools(example))
    assert list(two) == [first.name, second.name]
    for pool, other in ((first, second), (second, first)):
        for expression in (expression for panel in two[pool.name] for expression in queries(panel)):
            assert f'{rules.POOL}="{pool.name}"' in expression
            assert f'"{other.name}"' not in expression


def test_every_gpu_row_has_one_panel_for_each_of_the_seven_quantities(example: rhi_config.Config) -> None:
    series = published()
    every = [*rows(example).items(), *rows(two_gpu_pools(example)).items()]
    assert [pool for pool, _ in every] == ["coursework", "coursework", "second"]
    for pool, row in every:
        selectors = {name: rules.selector(series[name], {rules.POOL: pool}) for name in SEVEN}
        on = {name: [panel for panel in row if any(selectors[name] in one for one in queries(panel))] for name in SEVEN}
        assert {name: len(panels) for name, panels in on.items()} == dict.fromkeys(SEVEN, 1), pool
        # One quantity a panel, memory bandwidth first.
        assert [panel["title"] for panel in row] == [on[name][0]["title"] for name in SEVEN]

        for panel in row:
            # Each reads the pool's state, "no session" or "waiting for capacity", while the pool
            # does not serve, and shows a GPU's series only while it does.
            states = rules.state_queries(pool)
            assert [one for one in queries(panel) if one in states.values()] == list(states.values())
            shown = [one for one in queries(panel) if one not in states.values()]
            assert shown and all(one.endswith(rules.serving(pool, "")[len("(") :]) for one in shown)
        # The energy is the published counter's rate on each series, summed per instance.
        assert rules.rate(series[ENERGY], {rules.POOL: pool}, by=("instance",)) in queries(on[ENERGY][0])[0]
        # A gauge, and an Xid code above all, stays one series for each GPU of each instance.
        for name in (*GAUGES, XID):
            assert f"max by (pool, instance, gpu) ({selectors[name]})" in queries(on[name][0])[0]


def test_every_dcgm_name_queried_is_a_field_of_the_exporters_counters_file(example: rhi_config.Config) -> None:
    dashboard = built(example)
    assert check.violations(dashboard.document(), dashboard.metrics, example.pools) == []

    expressions = [one["expr"] for panel in dashboard.document()["panels"] for one in panel.get("targets", ())]
    queried = {name for expression in expressions for name in re.findall(r"DCGM_\w+", expression)}
    # What is queried is each field as the collector publishes it, and nothing else.
    assert queried == set(published().values())
    assert set(SEVEN) <= set(counter_types())
    assert set(dashboard.metrics) == {rules.EmittedBy(name, "services/engine/dcgm/counters.csv") for name in SEVEN}
