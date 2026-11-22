"""The fleet and scaling dashboard, built from the example configuration (system design, sections 7.2 and 7.3)."""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest

from rhi import config as rhi_config
from rhi_common.engine_metrics import Quantity
from rhi_dashboards import build, check, fleet, objectives, rules
from rhi_lifecycle import stages
from rhi_scaler import metrics as scaler

SCALER_FILE = "services/scaler/rhi_scaler/metrics.py"
LIFECYCLE_FILE = "services/engine/lifecycle/rhi_lifecycle/stages.py"
# The two transitions that put an instance in service: from the warm pool, and with none to promote.
STARTS = {stages.Transition.PROMOTION.value, stages.Transition.COLD_LAUNCH.value}


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


def built(config: rhi_config.Config) -> build.Built:
    """The fleet dashboard, as the package's build makes it."""
    (one,) = [one for one in build.dashboards(config) if one.module == fleet.__name__]
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


def panels_on(row: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    """The row's panels with a query on the metric."""
    return [panel for panel in row if any(metric in expression for expression in queries(panel))]


def test_each_pool_of_the_configuration_has_its_row(example: rhi_config.Config) -> None:
    assert list(rows(example)) == [fleet.OBJECTIVES_ROW, *(pool.name for pool in example.pools)]

    first = example.pools[0]
    second = dataclasses.replace(first, name="second")
    two = rows(dataclasses.replace(example, pools=(first, second)))

    assert list(two) == [fleet.OBJECTIVES_ROW, first.name, second.name]
    assert len(two[first.name]) == len(two[second.name]) == len(rows(example)[first.name])
    # Every query of a row is for the row's pool, and for no other.
    for pool, other in ((first, second), (second, first)):
        for expression in (expression for panel in two[pool.name] for expression in queries(panel)):
            assert f'{rules.POOL}="{pool.name}"' in expression
            assert f'"{other.name}"' not in expression


def test_a_row_shows_the_fleet_the_scale_decisions_and_each_instances_engine_load(example: rhi_config.Config) -> None:
    for pool in example.pools:
        row = rows(example)[pool.name]
        matchers = {rules.POOL: pool.name}

        # The desired, in-service and warm counts, as the scaler sends them.
        (counts,) = panels_on(row, scaler.DESIRED_CAPACITY)
        assert set(queries(counts)) == {rules.selector(gauge, matchers) for gauge in scaler.GAUGES}

        # The decisions, counted by reason: an increase of each series, then summed.
        (decisions,) = panels_on(row, scaler.DECISIONS)
        by_reason = rules.increase(scaler.DECISIONS, matchers, window=fleet.DECISIONS_WINDOW, by=("reason",))
        assert queries(decisions) == [by_reason]

        # The engine's running and waiting requests, in the names of the pool's engine, one series
        # an instance. With no instance in service the panel reads asleep.
        running = rules.engine_selector(pool, Quantity.RUNNING_REQUESTS)
        waiting = rules.engine_selector(pool, Quantity.WAITING_REQUESTS)
        (load,) = panels_on(row, running)
        assert panels_on(row, waiting) == [load]
        for series in (running, waiting):
            assert any(series in expression and "by (pool, instance)" in expression for expression in queries(load))
        assert rules.asleep(pool.name) in queries(load)


def test_a_row_shows_each_launchs_stage_seconds_with_promotions_and_cold_launches_apart(
    example: rhi_config.Config,
) -> None:
    for pool in example.pools:
        panels = panels_on(rows(example)[pool.name], stages.METRIC)
        shown = []
        for panel in panels:
            (expression,) = queries(panel)
            (transition,) = [one.value for one in stages.Transition if f'"{one.value}"' in expression]
            # The pool's launches of one transition: a series for each instance, a launch, and stage.
            assert rules.selector(stages.METRIC, {rules.POOL: pool.name, "transition": transition}) in expression
            assert "by (pool, instance, stage)" in expression
            shown.append(transition)

        assert len(panels) == 2
        assert set(shown) == STARTS


def test_every_query_is_on_a_metric_the_scaler_the_lifecycle_agent_or_the_engine_table_names(
    example: rhi_config.Config,
) -> None:
    dashboard = built(example)

    # Every name queried is declared, and every declared file holds its name: the rules of the build.
    assert check.violations(dashboard.document(), dashboard.metrics, example.pools) == []

    emitted = {one.name: one.path for one in dashboard.metrics if isinstance(one, rules.EmittedBy)}
    from_the_table = {one.quantity for one in dashboard.metrics if isinstance(one, rules.FromEngineTable)}
    assert len(emitted) + len(from_the_table) == len(dashboard.metrics)
    objectives_emitted = {one.name: one.path for one in objectives.METRICS if isinstance(one, rules.EmittedBy)}
    assert emitted == (
        dict.fromkeys((*scaler.GAUGES, scaler.DECISIONS), SCALER_FILE)
        | {stages.METRIC: LIFECYCLE_FILE}
        | objectives_emitted
    )
    assert from_the_table == {
        Quantity.RUNNING_REQUESTS,
        Quantity.WAITING_REQUESTS,
        Quantity.INPUT_TOKENS,
        Quantity.CACHED_INPUT_TOKENS,
    }
    # A name the module does not declare would be refused: the check reads the queries themselves.
    without_one = [one for one in dashboard.metrics if one != rules.EmittedBy(stages.METRIC, LIFECYCLE_FILE)]
    undeclared = check.violations(dashboard.document(), without_one, example.pools)
    assert {violation.rule for violation in undeclared} == {"undeclared"}


def test_the_objectives_row_shows_each_objective_against_its_target(example: rhi_config.Config) -> None:
    row = rows(example)[fleet.OBJECTIVES_ROW]
    measures = [measure for objective in objectives.objectives(example) for measure in objective.measures]
    assert len(row) == len(measures)
    for panel, measure in zip(row, measures, strict=True):
        assert panel["type"] == "stat"
        # The measure, and the count behind a percentile, each one value over the dashboard's range.
        assert queries(panel) == [measure.expression, *([measure.count] if measure.count else [])]
        assert all(target["instant"] for target in panel["targets"])
        # Red below the threshold, green from the least value that meets it.
        steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
        assert [step["color"] for step in steps] == ["red", "green"]
        green = steps[1]["value"]
        assert measure.met(green) and not measure.met(math.nextafter(green, -math.inf))
        # Every query keeps only the minutes the session gauge reads 1, over the dashboard's range.
        for expression in queries(panel):
            assert f"min_over_time({rules.SESSION_ON}[{rules.SESSION_STEP}]) == 1" in expression
            assert f"[{rules.SESSION_RANGE}:{rules.SESSION_STEP}]" in expression
    shown = {panel["title"]: panel["fieldConfig"]["defaults"]["thresholds"]["steps"][1]["value"] for panel in row}
    assert shown["Gateway answers: not the gateway's fault"] == 0.995
    assert shown["Cold start from the warm pool: under 5 minutes"] == 0.9
    # A cached share of exactly 85% is not above 85%: it is shown red.
    assert shown["Cached share of input: coursework"] > 0.85


def test_a_panel_states_the_target_it_applies(example: rhi_config.Config) -> None:
    config = dataclasses.replace(example, cached_share_flag_percent=85.4)
    objective = objectives.cached_share(config)
    panel = fleet.objective_stat(objective, objective.measures[0]).build()
    # The share it turns green above is the configured one.
    assert "above 85.4%" in (panel.description or "")
    assert objectives.threshold_step(objective.measures[0]) == math.nextafter(85.4 / 100, math.inf)
