"""The helpers every panel is built with (`rhi_dashboards.rules`; system design, sections 4.1 and 7.2)."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from grafana_foundation_sdk.builders import stat as sdk_stat
from grafana_foundation_sdk.builders import timeseries as sdk_timeseries
from grafana_foundation_sdk.cog.encoder import JSONEncoder

from rhi_common.engine_metrics import ENGINES, Kind, Quantity, metric_names
from rhi_common.pool_config import Pool, parse_pools
from rhi_dashboards import rules

REPO = Path(__file__).resolve().parents[3]

MOCK = parse_pools(
    {
        "mock": {
            "engine": "mock",
            "instance_types": ["t4g.small"],
            "prices": {"t4g.small": 0.02},
            "wake": "on-demand",
            "max_instances": 2,
            "warm_instances": 1,
            "target_concurrency": 8,
            "context_length": 262144,
            "largest_output": 32000,
        }
    }
)[0]

# The scaler's count of the pool's instances in service, as PromQL: what keeps an engine query
# (`A and on (pool) (count > 0)` has A's series only while the count is above zero), and the
# series that stands for the pool's state (`count == 0` has one series, of value zero, only while
# the count is zero).
KEPT_WHILE_IN_SERVICE = ') and on (pool) (rhi_scaler_instances_in_service{pool="mock"} > 0)'
ONLY_AT_ZERO = 'rhi_scaler_instances_in_service{pool="mock"} == 0'


def as_json(panel: Any) -> dict[str, Any]:
    return json.loads(json.dumps(panel.build(), cls=JSONEncoder))


def expressions(panel: dict[str, Any]) -> list[str]:
    return [target["expr"] for target in panel["targets"]]


def counter_arguments(expression: str) -> list[str]:
    """What each `rate` and `increase` of the expression is taken of. Fails when one is not a plain argument."""
    plain = re.findall(r"\b(?:rate|increase)\(([^()]*)\)", expression)
    assert len(plain) == len(re.findall(r"\b(?:rate|increase)\(", expression)), expression
    return plain


def shown(panel: dict[str, Any], in_service: int, last_reported: float) -> list[str]:
    """What the panel displays, one text per series, for a pool with `in_service` instances.

    `last_reported` is what every engine expression still returns: with no instance in service it
    is what an instance last said, which Prometheus keeps for some minutes.
    """
    texts = []
    for target in panel["targets"]:
        expression = target["expr"]
        if expression == ONLY_AT_ZERO:
            values = [0.0] if in_service == 0 else []
        elif expression.startswith("(") and expression.endswith(KEPT_WHILE_IN_SERVICE):
            values = [last_reported] if in_service > 0 else []
        else:
            values = [last_reported]
        mapped = {
            value: result["text"]
            for override in panel["fieldConfig"]["overrides"]
            if override["matcher"] == {"id": "byFrameRefID", "options": target["refId"]}
            for one in override["properties"]
            if one["id"] == "mappings"
            for mapping in one["value"]
            if mapping["type"] == "value"
            for value, result in mapping["options"].items()
        }
        texts += [mapped.get(f"{value:g}", f"{value:g}") for value in values]
    return texts


@pytest.mark.parametrize("function", ["rate", "increase"])
def test_a_rate_is_taken_of_each_series_and_then_summed_by_pool(function: str) -> None:
    helper = getattr(rules, function)

    expression = helper("rhi_gateway_student_requests_total", {"pool": "mock"}, window="5m", by=("alias",))

    series = 'rhi_gateway_student_requests_total{pool="mock"}[5m]'
    assert expression == f"sum by (pool, alias) ({function}({series}))"
    assert helper("up") == f"sum by (pool) ({function}(up[$__rate_interval]))"


def test_no_helper_takes_a_rate_or_an_increase_over_a_sum() -> None:
    written = [
        rules.rate("requests_total", {"pool": "mock"}),
        rules.increase("requests_total", by=("alias",)),
        rules.quantile(0.95, "request_seconds", {"pool": "mock"}),
        rules.request_count("request_seconds", {"pool": "mock"}),
        rules.engine_rate(MOCK, Quantity.OUTPUT_TOKENS),
        rules.engine_increase(MOCK, Quantity.TIME_TO_FIRST_TOKEN, "_count"),
        *expressions(as_json(rules.engine_percentile_timeseries(MOCK, "First token", Quantity.TIME_TO_FIRST_TOKEN))),
    ]

    taken_of = [argument for expression in written for argument in counter_arguments(expression)]

    # Every expression takes one, but the pool's state.
    assert [expression for expression in written if not counter_arguments(expression)] == [ONLY_AT_ZERO]
    for argument in taken_of:
        # One metric's series over a range: no aggregation and no subquery inside the function.
        assert re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_:]*(\{[^{}]*\})?\[[^\[\]:]+\]", argument), argument


@pytest.mark.parametrize("engine", ENGINES)
def test_an_engine_query_takes_its_names_from_the_row_of_the_pools_engine(engine: str) -> None:
    pool = dataclasses.replace(MOCK, engine=engine, name="some-pool")
    row = metric_names(engine)

    for quantity in Quantity:
        sample = {Kind.GAUGE: "", Kind.COUNTER: "_total", Kind.HISTOGRAM: "_bucket"}[quantity.kind]
        assert rules.engine_series(pool, quantity, sample) == row[quantity] + sample
        assert rules.engine_selector(pool, quantity, sample) == f'{row[quantity]}{sample}{{pool="some-pool"}}'
    assert f'{row[Quantity.OUTPUT_TOKENS]}_total{{pool="some-pool"}}' in rules.engine_rate(pool, Quantity.OUTPUT_TOKENS)


def test_two_engines_give_one_quantity_two_names(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {
        "mock": dict.fromkeys(Quantity, "first:tokens"),
        "other": dict.fromkeys(Quantity, "second_tokens"),
    }
    monkeypatch.setattr(rules, "metric_names", rows.__getitem__)

    first = rules.engine_rate(MOCK, Quantity.OUTPUT_TOKENS)
    second = rules.engine_rate(dataclasses.replace(MOCK, engine="other"), Quantity.OUTPUT_TOKENS)

    assert "first:tokens_total" in first
    assert "second_tokens_total" in second
    assert "first:tokens" not in second


def test_an_engine_with_no_row_and_a_histogram_with_no_sample_are_refused() -> None:
    with pytest.raises(KeyError):
        rules.engine_rate(dataclasses.replace(MOCK, engine="no-such-engine"), Quantity.OUTPUT_TOKENS)
    with pytest.raises(ValueError, match="histogram"):
        rules.engine_series(MOCK, Quantity.TIME_TO_FIRST_TOKEN)
    with pytest.raises(ValueError, match="histogram"):
        rules.engine_percentile_timeseries(MOCK, "Output", Quantity.OUTPUT_TOKENS)


def engine_panels(pool: Pool) -> dict[str, Any]:
    queries = {
        "tokens a second": rules.engine_rate(pool, Quantity.OUTPUT_TOKENS),
        "running": f"sum by (pool) ({rules.engine_selector(pool, Quantity.RUNNING_REQUESTS)})",
    }
    return {
        "graph": rules.engine_timeseries(pool, "Output", queries),
        "stat": rules.engine_stat(pool, "Output now", queries),
        "percentile": rules.engine_percentile_timeseries(pool, "First token", Quantity.TIME_TO_FIRST_TOKEN),
    }


@pytest.mark.parametrize("kind", ["graph", "stat", "percentile"])
def test_an_engine_panel_reads_asleep_when_the_scaler_counts_no_instance(kind: str) -> None:
    built = engine_panels(MOCK)[kind]
    panel = as_json(built)

    assert isinstance(built, sdk_stat.Panel if kind == "stat" else sdk_timeseries.Panel)
    assert ONLY_AT_ZERO in expressions(panel)
    engine = [expression for expression in expressions(panel) if expression != ONLY_AT_ZERO]
    assert engine
    assert all(expression.startswith("(") and expression.endswith(KEPT_WHILE_IN_SERVICE) for expression in engine)

    # At zero the panel reads asleep and nothing else, whatever an instance last reported.
    assert shown(panel, in_service=0, last_reported=17.0) == ["asleep"]
    # In service it shows the engine's numbers, and an engine's own zero stays a number.
    assert shown(panel, in_service=2, last_reported=17.0) == ["17"] * len(engine)
    assert shown(panel, in_service=1, last_reported=0.0) == ["0"] * len(engine)


def test_a_stat_asks_for_the_value_now_and_a_graph_for_the_range() -> None:
    panels = engine_panels(MOCK)

    # A stat over the range would show the last value an instance reported before the pool slept.
    assert all(target["instant"] and not target["range"] for target in as_json(panels["stat"])["targets"])
    assert all(target["range"] and not target["instant"] for target in as_json(panels["graph"])["targets"])


@pytest.mark.parametrize("window", ["5m", "$__rate_interval"])
def test_a_percentile_panel_carries_the_count_of_requests_over_the_same_window(window: str) -> None:
    matchers = {"pool": "mock", "wire_format": "messages"}
    selector = '{pool="mock", wire_format="messages"}'
    plain = rules.percentile_timeseries("First token", "rhi_first_token_seconds", matchers, window=window)
    of_engine = rules.engine_percentile_timeseries(MOCK, "First token", Quantity.TIME_TO_FIRST_TOKEN, window=window)
    family = metric_names("mock")[Quantity.TIME_TO_FIRST_TOKEN]

    for panel, histogram, series in (
        (as_json(plain), "rhi_first_token_seconds", selector),
        (as_json(of_engine), family, '{pool="mock"}'),
    ):
        quantiles = [expression for expression in expressions(panel) if "histogram_quantile(" in expression]
        counts = [expression for expression in expressions(panel) if f"increase({histogram}_count" in expression]
        assert len(quantiles) == 2
        assert all(f"rate({histogram}_bucket{series}[{window}])" in expression for expression in quantiles)
        assert len(counts) == 1
        assert f"sum by (pool) (increase({histogram}_count{series}[{window}]))" in counts[0]


def test_every_query_names_its_data_source_by_the_identifier_the_image_provisions() -> None:
    provisioned = yaml.safe_load(
        (REPO / "services/grafana/provisioning/datasources/datasources.yaml").read_text(encoding="utf-8")
    )
    assert {rules.PROMETHEUS.uid: rules.PROMETHEUS.type_val, rules.CLOUDWATCH.uid: rules.CLOUDWATCH.type_val} == {
        source["uid"]: source["type"] for source in provisioned["datasources"]
    }

    prometheus = {"type": "prometheus", "uid": rules.PROMETHEUS_UID}
    panels = [*engine_panels(MOCK).values(), rules.percentile_timeseries("First token", "rhi_first_token_seconds")]
    for panel in map(as_json, panels):
        assert [target["datasource"] for target in panel["targets"]] == [prometheus] * len(panel["targets"])
