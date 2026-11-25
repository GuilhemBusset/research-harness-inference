"""The fleet and scaling dashboard, built from the example configuration (system design, sections 7.2 and 7.3)."""

from __future__ import annotations

import dataclasses
import json
import math
from typing import Any

import pytest
from grafana_foundation_sdk.cog.encoder import JSONEncoder

from rhi import config as rhi_config
from rhi_common.engine_metrics import MOCK, Quantity
from rhi_dashboards import build, check, fleet, gpu, objectives, rules
from rhi_lifecycle import stages
from rhi_scaler import metrics as scaler

SCALER_FILE = "services/scaler/rhi_scaler/metrics.py"
LIFECYCLE_FILE = "services/engine/lifecycle/rhi_lifecycle/stages.py"
DCGM_FILE = "services/engine/dcgm/counters.csv"
# The scaler's counts of a pool's instances, in the row's order: requested, asked of AWS, waiting
# for capacity, starting and in service.
COUNTS = (
    scaler.INSTANCES_REQUESTED,
    scaler.DESIRED_CAPACITY,
    scaler.INSTANCES_WAITING,
    scaler.INSTANCES_STARTING,
    scaler.INSTANCES_IN_SERVICE,
)
SESSION_WINDOW = (scaler.SESSION_STARTS_AT, scaler.SESSION_ENDS_AT)
# The GPU's fields of the table: utilization, memory used and power.
GPU_FIELDS = (gpu.COMPUTE, gpu.MEMORY_USED, gpu.POWER)
# The two transitions that put an instance in service, as the lifecycle agent names them.
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


def state_queries(panel: dict[str, Any], pool: str) -> list[str]:
    """The panel's queries of the pool's two states, "no session" and "waiting for capacity"."""
    states = rules.state_queries(pool).values()
    return [expression for expression in queries(panel) if expression in states]


def test_a_row_shows_what_the_session_requested_against_what_is_available(example: rhi_config.Config) -> None:
    for pool in example.pools:
        row = rows(example)[pool.name]
        matchers = {rules.POOL: pool.name}

        # The session: whether it is active, and its start and its end, as the scaler reads them now.
        (session,) = panels_on(row, scaler.SESSION_STARTS_AT)
        assert all(target["instant"] for target in session["targets"])
        for gauge in (scaler.SESSION_ACTIVE, *SESSION_WINDOW):
            assert any(rules.selector(gauge, matchers) in expression for expression in queries(session))

        # Over time, the five counts, as the scaler sends them, and no other.
        (counts,) = panels_on(row, scaler.INSTANCES_REQUESTED)
        assert counts["type"] == "timeseries"
        assert queries(counts) == [rules.selector(gauge, matchers) for gauge in COUNTS]
        assert all(target["range"] for target in counts["targets"])

        # The seconds the pool has waited, and the cause of its last failed launch: the one in
        # force, of the scaler's series for each cause, read by its cause.
        (waiting,) = panels_on(row, scaler.WAITING_SECONDS)
        assert panels_on(row, scaler.LAUNCH_FAILURE) == [waiting]
        assert rules.selector(scaler.WAITING_SECONDS, matchers) in queries(waiting)
        (cause,) = [target for target in waiting["targets"] if scaler.LAUNCH_FAILURE in target["expr"]]
        assert cause["expr"] == f"{rules.selector(scaler.LAUNCH_FAILURE, matchers)} == 1"
        assert "{{cause}}" in cause["legendFormat"]

        # The decisions, counted by reason: an increase of each series, then summed.
        (decisions,) = panels_on(row, scaler.DECISIONS)
        by_reason = rules.increase(scaler.DECISIONS, matchers, window=fleet.DECISIONS_WINDOW, by=("reason",))
        assert queries(decisions) == [by_reason]

        # The engine's running and waiting requests, in the names of the pool's engine, one series
        # an instance. While the pool does not serve, the panel reads the pool's state.
        running = rules.engine_selector(pool, Quantity.RUNNING_REQUESTS)
        waiting_requests = rules.engine_selector(pool, Quantity.WAITING_REQUESTS)
        (load,) = [panel for panel in panels_on(row, running) if panel["type"] == "timeseries"]
        for series in (running, waiting_requests):
            assert any(series in expression and "by (pool, instance)" in expression for expression in queries(load))
        assert state_queries(load, pool.name) == list(rules.state_queries(pool.name).values())


def test_under_them_a_table_has_one_line_for_each_instance_with_its_telemetry(example: rhi_config.Config) -> None:
    assert {pool.engine == MOCK for pool in example.pools} == {True, False}
    for pool in example.pools:
        row = rows(example)[pool.name]
        (table,) = [panel for panel in row if panel["type"] == "table"]
        assert row[-1] is table
        # Each column is the value now, read as a table.
        assert all(target["instant"] and target["format"] == "table" for target in table["targets"])
        # While the pool does not serve, the table reads the pool's state, and nothing an instance last reported.
        states = state_queries(table, pool.name)
        assert states == list(rules.state_queries(pool.name).values())
        columns = [expression for expression in queries(table) if expression not in states]
        kept = rules.serving(pool.name, "")[len("(") :]
        assert all(expression.endswith(kept) for expression in columns)
        # One line an instance: every column has one series for each instance of the pool.
        assert all("by (pool, instance" in expression for expression in columns)

        def one(series: str, columns: list[str] = columns) -> str:
            """The one column that reads the series."""
            (found,) = [expression for expression in columns if series in expression]
            return found

        # The engine's columns: its running and waiting requests, its output tokens a second, and
        # its time to first token, the median and the 95th percentile with the requests behind them.
        by_instance = ("instance",)
        first_token = Quantity.TIME_TO_FIRST_TOKEN
        engine = [
            one(rules.engine_selector(pool, Quantity.RUNNING_REQUESTS)),
            one(rules.engine_selector(pool, Quantity.WAITING_REQUESTS)),
            one(rules.engine_rate(pool, Quantity.OUTPUT_TOKENS, by=by_instance)),
            one(rules.engine_quantile(pool, 0.5, first_token, by=by_instance)),
            one(rules.engine_quantile(pool, 0.95, first_token, by=by_instance)),
            one(rules.engine_request_count(pool, first_token, by=by_instance)),
        ]
        # The GPU's: utilization, memory used and power. A mock instance has no GPU, and no such column.
        of_gpu = [expression for expression in columns if "DCGM_" in expression]
        if pool.engine == MOCK:
            assert of_gpu == []
        else:
            assert of_gpu == [one(rules.selector(field, {rules.POOL: pool.name})) for field in GPU_FIELDS]
        assert columns == [*of_gpu, *engine]
        # Every column is read under a header of its own.
        headers = {
            override["matcher"]["options"]: prop["value"]
            for override in table["fieldConfig"]["overrides"]
            for prop in override["properties"]
            if prop["id"] == "displayName"
        }
        named = [headers[rules.table_value(target["refId"])] for target in table["targets"] if target["expr"] in columns]
        assert len(set(named)) == len(columns)


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
    of_the_scaler = (*COUNTS, *SESSION_WINDOW, scaler.WAITING_SECONDS, scaler.LAUNCH_FAILURE, scaler.DECISIONS)
    assert emitted == (
        dict.fromkeys(of_the_scaler, SCALER_FILE)
        | {stages.METRIC: LIFECYCLE_FILE}
        | dict.fromkeys(GPU_FIELDS, DCGM_FILE)
        | objectives_emitted
    )
    assert from_the_table == {
        Quantity.RUNNING_REQUESTS,
        Quantity.WAITING_REQUESTS,
        Quantity.OUTPUT_TOKENS,
        Quantity.TIME_TO_FIRST_TOKEN,
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
        # Every query counts only the samples taken in the dashboard's range while the gauge of the
        # measure's session read 1, its pool's or any pool's, at every step of that range.
        for expression in queries(panel):
            read = check._read(expression)
            assert f"[{rules.SESSION_RANGE}:{rules.SESSION_STEP}]" in expression
            counted = [one for one in read.selectors if one.name != rules.SESSION_ACTIVE]
            assert counted and all(check._counted_per_sample(read, one, measure.pool) for one in counted)
    shown = {panel["title"]: panel["fieldConfig"]["defaults"]["thresholds"]["steps"][1]["value"] for panel in row}
    # The four objectives of the design, and none on the time a launch takes.
    assert list(dict.fromkeys(title.split(":")[0] for title in shown)) == [
        "Gateway answers",
        "Time to first token",
        "Output speed per stream",
        "Cached share of input",
    ]
    assert not any(stages.LAUNCH_METRIC in expression for panel in row for expression in queries(panel))
    assert shown["Gateway answers: not the gateway's fault"] == 0.995
    # A cached share of exactly 85% is not above 85%: it is shown red.
    assert shown["Cached share of input: coursework"] > 0.85


def test_a_panel_states_the_target_it_applies(example: rhi_config.Config) -> None:
    config = dataclasses.replace(example, cached_share_flag_percent=85.4)
    objective = objectives.cached_share(config)
    panel = fleet.objective_stat(objective, objective.measures[0]).build()
    # The share it turns green above is the configured one.
    assert "above 85.4%" in (panel.description or "")
    assert objectives.threshold_step(objective.measures[0]) == math.nextafter(85.4 / 100, math.inf)


def test_a_share_older_buckets_only_bound_reads_inconclusive(example: rhi_config.Config) -> None:
    def mapped(config: rhi_config.Config) -> dict[str, dict[str, Any]]:
        """What the first-token panel shows instead of each value of its measure's query."""
        objective = objectives.first_token(config)
        panel = json.loads(json.dumps(fleet.objective_stat(objective, objective.measures[0]).build(), cls=JSONEncoder))
        return {
            value: result
            for override in panel["fieldConfig"].get("overrides", ())
            if override["matcher"] == {"id": "byFrameRefID", "options": fleet.MEASURE_REF}
            for one in override["properties"]
            if one["id"] == "mappings"
            for mapping in one["value"]
            for value, result in mapping["options"].items()
        }

    # A floor that is not one of the gateway's own boundaries: neither red nor green where older
    # buckets straddle it (`objectives.INCONCLUSIVE`).
    revised = dataclasses.replace(example, target_ttft_p95_seconds=6)
    result = mapped(revised)[f"{objectives.INCONCLUSIVE:g}"]
    assert (result["text"], result["color"]) == ("inconclusive", "orange")
    assert check.violations(built(revised).document(), built(revised).metrics, revised.pools) == []
    # The example's floor is one of them: its share is read exactly, and always gives a verdict.
    assert mapped(example) == {}
