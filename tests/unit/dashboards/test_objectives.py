"""The five objectives of section 7.3, measured while a session is on (`rhi_dashboards.objectives`)."""

from __future__ import annotations

import dataclasses

import pytest

from rhi import config as rhi_config
from rhi_common.engine_metrics import Quantity, metric_names
from rhi_dashboards import check, objectives, rules
from rhi_gateway import experience
from rhi_gateway import metrics as gateway
from rhi_lifecycle import stages
from rhi_scaler import metrics as scaler

GATE = f"and on () ({rules.SESSION_ON} == 1)"


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


def by_name(config: rhi_config.Config) -> dict[str, objectives.Objective]:
    return {objective.name: objective for objective in objectives.objectives(config)}


def selectors(expression: str) -> dict[str, list[dict[str, str]]]:
    """The metrics the expression reads, each with the equality matchers of each of its selectors."""
    found: dict[str, list[dict[str, str]]] = {}
    for one in check._read(expression).selectors:
        found.setdefault(one.name, []).append({label: value for label, op, value in one.matchers if op == "="})
    return found


def ungated(expression: str) -> list[str]:
    """The metrics the expression reads outside a span kept to the session over the dashboard's range."""
    read = check._read(expression)
    sessions = [one for one in check._sessions(read.tokens) if one.range == f"{rules.SESSION_RANGE}:{rules.SESSION_STEP}"]
    return [
        one.name
        for one in read.selectors
        if one.name != rules.SESSION_ON and check._session_of(sessions, one.start, one.end) is None
    ]


def test_five_objectives_with_the_example_targets(example: rhi_config.Config) -> None:
    found = by_name(example)
    assert len(found) == 5
    targets = {name: [(m.target, m.comparison) for m in objective.measures] for name, objective in found.items()}
    at_least, above, under = objectives.Comparison.AT_LEAST, objectives.Comparison.ABOVE, objectives.Comparison.UNDER
    assert targets == {
        "Gateway answers": [(0.995, at_least)],
        "Time to first token, warm pool": [(5, under), (5, under)],
        "Output speed per stream, warm pool": [(15, at_least), (10, at_least)],
        "Cached share of input": [(0.85, above)] * len(example.pools),
        "Cold start from the warm pool": [(300, under)],
    }
    # The 95th percentile of the first token, the median and the slowest tenth of the speed, the
    # 90th percentile of the cold start.
    first_token, speed, cold = (found[name].queries for name in targets if name.startswith(("Time", "Output", "Cold")))
    assert all(query.startswith("histogram_quantile(0.95, ") for query in first_token)
    assert [query.split(",")[0] for query in speed] == ["histogram_quantile(0.5", "histogram_quantile(0.1"]
    assert cold[0].startswith("quantile(0.9, ")


def test_every_query_is_limited_by_the_session_gauge(example: rhi_config.Config) -> None:
    measures = [measure for objective in objectives.objectives(example) for measure in objective.measures]
    queries = [query for measure in measures for query in (measure.expression, measure.count) if query]
    assert len(queries) > len(measures)
    for query in queries:
        assert GATE in query
        assert ungated(query) == [], query
    # The gauge the queries read is the one the scaler sends.
    assert rules.SESSION_ON == scaler.SESSION_ON


def test_first_turns_apart(example: rhi_config.Config) -> None:
    first_token = by_name(example)["Time to first token, warm pool"]
    assert len(first_token.queries) == 2
    turns = []
    for measure in first_token.measures:
        read = selectors(measure.expression)
        assert set(read) == {f"{experience.FIRST_TOKEN}_bucket", rules.SESSION_ON}
        (matchers,) = read[f"{experience.FIRST_TOKEN}_bucket"]
        # Warm requests only: those that were not held.
        assert matchers["start"] == "warm"
        turns.append(matchers["turn"])
        # The count behind it is of the same requests.
        assert selectors(measure.count or "")[f"{experience.FIRST_TOKEN}_count"] == [matchers]
    assert turns == ["first", "later"]


def test_the_targets_follow_the_configuration(example: rhi_config.Config) -> None:
    other = dataclasses.replace(
        example,
        target_ttft_p95_seconds=4,
        target_stream_median_tokens_per_second=20,
        target_stream_p10_tokens_per_second=12.5,
        cached_share_flag_percent=90,
    )
    found = {name: [measure.target for measure in one.measures] for name, one in by_name(other).items()}
    assert found["Time to first token, warm pool"] == [4, 4]
    assert found["Output speed per stream, warm pool"] == [20, 12.5]
    assert found["Cached share of input"] == [0.9] * len(example.pools)
    # The design's two are not the configuration's.
    assert found["Gateway answers"] == [0.995]
    assert found["Cold start from the warm pool"] == [300]


def test_the_gateway_counts_as_its_fault_a_5xx_that_names_no_cause(example: rhi_config.Config) -> None:
    (measure,) = by_name(example)["Gateway answers"].measures
    read = selectors(measure.expression)
    # The gateway's own faults over its requests, by the names the gateway serves them under.
    assert set(read) == {gateway.OWN_FAULTS + "_total", gateway.REQUESTS + "_total", rules.SESSION_ON}
    # Summed whatever their labels: no pool, no student.
    assert all(matchers == {} for name, each in read.items() if name != rules.SESSION_ON for matchers in each)
    faults = rules.over_sessions(rules.minute_increase(objectives.OWN_FAULTS))
    requests = rules.over_sessions(rules.minute_increase(objectives.REQUESTS))
    # Where no fault was counted, zero faults are read: the share of a session without one is 1.
    assert measure.expression == f"1 - (({faults}) or on () (0 * {requests})) / ({requests})"


def test_the_speed_is_measured_for_each_pool_whose_engine_is_not_the_mock(example: rhi_config.Config) -> None:
    gpu = next(pool for pool in example.pools if pool.engine != "mock")
    second = dataclasses.replace(gpu, name="second")
    config = dataclasses.replace(example, pools=(*example.pools, second))
    speed = by_name(config)["Output speed per stream, warm pool"]
    read = [selectors(query)[f"{experience.SPEED}_bucket"] for query in speed.queries]
    assert read == [[{"pool": gpu.name, "start": "warm"}]] * 2 + [[{"pool": "second", "start": "warm"}]] * 2
    assert all(pool.engine != "mock" for pool in objectives.speed_pools(config))
    assert [pool.name for pool in objectives.speed_pools(config)] == [gpu.name, "second"]


def test_the_cached_share_is_each_pools_engine_kept_to_the_pool_in_service(example: rhi_config.Config) -> None:
    shares = by_name(example)["Cached share of input"].measures
    assert [measure.legend for measure in shares] == [pool.name for pool in example.pools]
    for pool, measure in zip(example.pools, shares, strict=True):
        names = metric_names(pool.engine)
        cached, total = (f"{names[quantity]}_total" for quantity in (Quantity.CACHED_INPUT_TOKENS, Quantity.INPUT_TOKENS))
        numerator, denominator = measure.expression.split(f"[{rules.SESSION_RANGE}:{rules.SESSION_STEP}]) / ")
        assert cached in numerator and total in denominator
        assert rules.in_service(pool.name) in numerator and rules.in_service(pool.name) in denominator


def test_the_cold_start_is_the_launches_promoted_from_the_warm_pool(example: rhi_config.Config) -> None:
    (measure,) = by_name(example)["Cold start from the warm pool"].measures
    (matchers,) = selectors(measure.expression)[stages.METRIC]
    assert matchers == {"transition": stages.Transition.PROMOTION.value}
    # A launch's stages summed, its largest sum while the session was on.
    assert "max_over_time(" in measure.expression and "sum by (pool, instance)" in measure.expression
