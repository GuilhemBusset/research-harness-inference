"""The five objectives of section 7.3, measured while a session is on (`rhi_dashboards.objectives`)."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry

from rhi import config as rhi_config
from rhi_common.engine_metrics import Quantity, metric_names
from rhi_dashboards import check, objectives, rules
from rhi_gateway import experience
from rhi_gateway import metrics as gateway
from rhi_lifecycle import stages
from rhi_scaler import metrics as scaler

# Every reading of the gauge in the minute: a minute counted only if each one was 1.
GATE = f"min_over_time({rules.SESSION_ON}[{rules.SESSION_STEP}]) == 1"
SUBQUERY = f"[{rules.SESSION_RANGE}:{rules.SESSION_STEP}]"
COLLECTORS = ("services/gateway/collector/config.yaml", "services/engine/collector/config.yaml")
AT_LEAST, ABOVE = objectives.Comparison.AT_LEAST, objectives.Comparison.ABOVE


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
    """The metrics the expression reads outside an expression that a session span gates over every
    minute of the dashboard's range: each read's innermost such span, with its range and step."""
    read = check._read(expression)
    sessions = check._sessions(read.tokens)
    subquery = check._tokens(SUBQUERY)[0]
    found = []
    for one in read.selectors:
        if one.name == rules.SESSION_ON:
            continue
        around = [span for span in sessions if span.inner_start <= one.start and one.end <= span.inner_end]
        innermost = max(around, key=lambda span: span.inner_start, default=None)
        # The span ends with its subquery's range and step, then the call's closing bracket.
        if innermost is None or list(read.tokens[innermost.end - 1 - len(subquery) : innermost.end - 1]) != subquery:
            found.append(one.name)
    return found


def test_five_objectives_with_the_example_targets(example: rhi_config.Config) -> None:
    found = by_name(example)
    judged = {
        name: [(m.target, m.percentile, m.comparison, m.threshold) for m in objective.measures]
        for name, objective in found.items()
    }
    # 99.5%; 5 seconds at the 95th percentile; 15 and 10 tokens per second, the median and the
    # slowest tenth; 85%; 5 minutes at the 90th percentile.
    assert judged == {
        "Gateway answers": [(0.995, None, AT_LEAST, 0.995)],
        "Time to first token, warm pool": [(5, 0.95, AT_LEAST, 0.95)] * 2,
        "Output speed per stream, warm pool": [(15, 0.5, ABOVE, 0.5), (10, 0.1, ABOVE, 0.9)],
        "Cached share of input": [(0.85, None, ABOVE, 0.85)] * len(example.pools),
        "Cold start from the warm pool": [(300, 0.9, AT_LEAST, 0.9)],
    }


def test_every_query_is_limited_by_the_session_gauge(example: rhi_config.Config) -> None:
    measures = [measure for objective in objectives.objectives(example) for measure in objective.measures]
    queries = [query for measure in measures for query in (measure.expression, measure.count) if query]
    assert len(queries) > len(measures)
    for query in queries:
        assert GATE in query and SUBQUERY in query
        assert ungated(query) == [], query
    # The gauge the queries read is the one the scaler sends.
    assert rules.SESSION_ON == scaler.SESSION_ON


def test_a_counter_counts_each_step_from_the_step_before() -> None:
    (expression,) = objectives.gateway_answers().queries
    # No window of its own: adjacent windows would drop the increment between their scrapes, and one
    # ending at a step would bring in what came before the session.
    assert "increase(" not in expression and "rate(" not in expression
    step = rules.minute_increase(objectives.REQUESTS)
    series = objectives.REQUESTS
    # Each sample less the one before, or all of it after a restart: summed over the minute's
    # sample steps, so that a restart between the minute's ends is still read as one.
    each = f"sum without () ((({series} - {series} offset {rules.SAMPLE_STEP}) >= 0) or {series})"
    assert step == f"sum by () (sum_over_time(({each})[{rules.SESSION_STEP}:{rules.SAMPLE_STEP}]))"
    assert rules.over_sessions(step) in expression


def seconds(duration: str) -> int:
    number, unit = re.fullmatch(r"([0-9]+)([sm])", duration).groups()  # ty: ignore[unresolved-attribute] - a test's own duration
    return int(number) * {"s": 1, "m": 60}[unit]


def test_a_sample_step_holds_at_most_one_scrape() -> None:
    # Every collector that writes a series the objectives read scrapes less often than a sample step.
    intervals = [seconds(one) for path in COLLECTORS for one in re.findall(r"scrape_interval: (\S+)", Path(path).read_text())]
    assert intervals and min(intervals) > seconds(rules.SAMPLE_STEP)
    assert seconds(rules.SESSION_STEP) % seconds(rules.SAMPLE_STEP) == 0


def test_a_minute_is_kept_only_if_every_reading_of_the_gauge_in_it_is_on() -> None:
    gate = rules.in_session("x")
    gauge = rules.SESSION_ON
    # One reading off between two on ones takes the minute out; the readings at its ends do not decide it.
    assert f"min_over_time({gauge}[{rules.SESSION_STEP}]) == 1" in gate
    assert "offset" not in gate
    # A minute in which the scaler did not run takes the last reading before it.
    assert f"({gauge} == 1) unless on () (count_over_time({gauge}[{rules.SESSION_STEP}]))" in gate


def test_first_turns_apart(example: rhi_config.Config) -> None:
    first_token = by_name(example)["Time to first token, warm pool"]
    assert len(first_token.queries) == 2
    turns = []
    for measure in first_token.measures:
        read = selectors(measure.expression)
        assert set(read) == {f"{experience.FIRST_TOKEN}_bucket", f"{experience.FIRST_TOKEN}_count", rules.SESSION_ON}
        matchers = read[f"{experience.FIRST_TOKEN}_bucket"][0]
        # Warm requests only, those that were not held, under the target's boundary...
        assert matchers.pop("le") == "5"
        assert matchers["start"] == "warm"
        turns.append(matchers["turn"])
        # ...over all of the same requests, which the panel shows beside it.
        assert read[f"{experience.FIRST_TOKEN}_count"][0] == matchers
        assert selectors(measure.count or "")[f"{experience.FIRST_TOKEN}_count"][0] == matchers
    assert turns == ["first", "later"]


def test_the_targets_follow_the_configuration(example: rhi_config.Config) -> None:
    other = dataclasses.replace(
        example,
        target_ttft_p95_seconds=4,
        target_stream_median_tokens_per_second=20,
        target_stream_p10_tokens_per_second=12.5,
        cached_share_flag_percent=90,
    )
    found = by_name(other)
    targets = {name: [measure.target for measure in one.measures] for name, one in found.items()}
    assert targets["Time to first token, warm pool"] == [4, 4]
    assert targets["Output speed per stream, warm pool"] == [20, 12.5]
    assert targets["Cached share of input"] == [0.9] * len(example.pools)
    # The design's two are not the configuration's.
    assert targets["Gateway answers"] == [0.995]
    assert targets["Cold start from the warm pool"] == [300]
    # And the shares are read at the configured boundaries.
    speed = found["Output speed per stream, warm pool"].queries
    assert [selectors(query)[f"{experience.SPEED}_bucket"][0]["le"] for query in speed] == ["20", "12.5"]
    first_token = found["Time to first token, warm pool"].queries[0]
    assert selectors(first_token)[f"{experience.FIRST_TOKEN}_bucket"][0]["le"] == "4"
    # The panel says the target it applies, as configured.
    (cached, *_) = found["Cached share of input"].measures
    assert "above 90% " in cached.goal
    assert "above 85.4% " in objectives.cached_share(dataclasses.replace(other, cached_share_flag_percent=85.4)).measures[0].goal


def test_a_target_between_two_boundaries_is_read_between_their_buckets(tmp_path: Path) -> None:
    # A revised target the configuration takes, whose boundary the gateway does not count.
    text = Path("config.example.toml").read_text(encoding="utf-8")
    for key, value in (("target_ttft_p95_seconds", "6"), ("target_stream_median_tokens_per_second", "16")):
        text = re.sub(rf"(?m)^{key} = .*$", f"{key} = {value}", text)
    (tmp_path / "revised.toml").write_text(text, encoding="utf-8")
    config = rhi_config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(tmp_path / "revised.toml")})
    found = by_name(config)
    for objective, histogram, matchers, lower, upper, measure in (
        ("Time to first token, warm pool", experience.FIRST_TOKEN, {"start": "warm", "turn": "first"}, 5, 7.5, 0),
        ("Output speed per stream, warm pool", experience.SPEED, {"pool": "coursework", "start": "warm"}, 15, 17.5, 0),
    ):
        expression = found[objective].measures[measure].expression
        bucket, count = (
            rules.over_sessions(rules.minute_increase(f"{histogram}_{kind}", {**matchers, **extra}))
            for kind, extra in (("bucket", {"le": objectives.boundary(lower)}), ("count", {}))
        )
        high = rules.over_sessions(rules.minute_increase(f"{histogram}_bucket", {**matchers, "le": objectives.boundary(upper)}))
        # The two buckets the gateway counts around it, the target 0.4 of the way from the lower.
        under = f"({bucket}) + (({high}) - ({bucket})) * 0.4"
        assert expression in (f"({under}) / ({count})", f"(({count}) - ({under})) / ({count})")


@pytest.mark.parametrize(("target", "selected"), [(4, {"4"}), (6, {"5", "7.5"}), (0.1, {"0.25"}), (2000, {"1800"})])
def test_a_first_token_target_is_read_at_the_gateways_boundaries(
    example: rhi_config.Config, target: float, selected: set[str]
) -> None:
    # At a boundary, its bucket; between two, both; under the first, the first; beyond the last, the last.
    first, _ = objectives.first_token(dataclasses.replace(example, target_ttft_p95_seconds=target)).measures
    read = {one["le"] for one in selectors(first.expression)[f"{experience.FIRST_TOKEN}_bucket"]}
    assert read == selected
    assert read <= {objectives.boundary(bound) for bound in experience.WAIT_BUCKETS}


def test_a_percentile_is_judged_exactly_at_its_floor(example: rhi_config.Config) -> None:
    found = by_name(example)
    first, _ = found["Time to first token, warm pool"].measures
    median, tenth = found["Output speed per stream, warm pool"].measures
    (cold,) = found["Cold start from the warm pool"].measures
    # Every first token at exactly 5 seconds: none under 5, so the 95th percentile is not under 5.
    assert not first.met(0.0)
    assert first.met(0.95) and not first.met(0.9499)
    # Every stream at exactly 15 tokens per second: all at 15 or more, so the median is.
    assert median.met(1.0)
    # Half the streams under 15: the median, the lower of the two middle ones, is under it.
    assert not median.met(0.5)
    assert tenth.met(0.91) and not tenth.met(0.9)
    # Launches of 100 and 300 seconds: half under 5 minutes, so the 90th percentile is not.
    assert not cold.met(0.5)


def test_a_boundary_is_selected_as_the_collector_writes_it() -> None:
    # Go's FormatFloat(bound, 'f', -1, 64): no ".0", no exponent, the fewest digits.
    assert [objectives.boundary(value) for value in (5, 5.0, 15, 12.5, 0.25, 300, 1800, 1e-05, 1e16)] == [
        "5", "5", "15", "12.5", "0.25", "300", "1800", "0.00001", "10000000000000000"
    ]


def test_the_example_floors_are_boundaries_the_gateway_counts_strictly_under(example: rhi_config.Config) -> None:
    registry = CollectorRegistry()
    made = experience.Experience(registry)
    made.speed.labels("coursework", "messages", "warm").observe(1)
    made.first_token.labels("coursework", "messages", "warm", "first").observe(1)
    # The boundaries the shares read are the gateway's and the lifecycle agent's, in the
    # workspace's spelling of them.
    served = {
        (sample.name, objectives.boundary(float(sample.labels["le"])))
        for family in registry.collect()
        for sample in family.samples
        if "le" in sample.labels and sample.labels["le"] != "+Inf"
    } | {(f"{stages.LAUNCH_METRIC}_bucket", objectives.boundary(bound)) for bound in stages.LAUNCH_BUCKETS}
    selected = set()
    for objective in by_name(example).values():
        for measure in objective.measures:
            for name, each in selectors(measure.expression).items():
                selected |= {(name, one["le"]) for one in each if "le" in one}
    # 5 seconds, 15 and 10 tokens a second, and 5 minutes.
    assert len(selected) == 4
    assert selected <= served
    # Each floor of the example is a boundary of its histogram.
    assert example.target_ttft_p95_seconds in experience.WAIT_BUCKETS
    assert {example.target_stream_median_tokens_per_second, example.target_stream_p10_tokens_per_second} <= set(
        experience.SPEED_BUCKETS
    )


def test_the_gateway_counts_as_its_fault_a_5xx_that_names_no_cause(example: rhi_config.Config) -> None:
    (measure,) = by_name(example)["Gateway answers"].measures
    read = selectors(measure.expression)
    # The gateway's own faults over its requests, by the names the gateway serves them under.
    assert set(read) == {gateway.OWN_FAULTS + "_total", gateway.REQUESTS + "_total", rules.SESSION_ON}
    # Summed whatever their labels: no pool, no student.
    assert all(matchers == {} for name, each in read.items() if name != rules.SESSION_ON for matchers in each)
    assert "sum by () (" in measure.expression
    faults = rules.over_sessions(rules.minute_increase(objectives.OWN_FAULTS))
    requests = rules.over_sessions(rules.minute_increase(objectives.REQUESTS))
    # Where no fault was counted, zero faults are read: the share of a session without one is 1.
    assert measure.expression == f"(({requests}) - (({faults}) or on () (0 * ({requests})))) / ({requests})"


def test_the_speed_is_measured_for_each_pool_whose_engine_is_not_the_mock(example: rhi_config.Config) -> None:
    gpu = next(pool for pool in example.pools if pool.engine != "mock")
    second = dataclasses.replace(gpu, name="second")
    config = dataclasses.replace(example, pools=(*example.pools, second))
    speed = by_name(config)["Output speed per stream, warm pool"]
    read = [selectors(query)[f"{experience.SPEED}_count"][0] for query in speed.queries]
    assert read == [{"pool": gpu.name, "start": "warm"}] * 2 + [{"pool": "second", "start": "warm"}] * 2
    assert [pool.name for pool in objectives.speed_pools(config)] == [gpu.name, "second"]


def test_the_cached_share_is_each_pools_engine_kept_to_the_pool_in_service(example: rhi_config.Config) -> None:
    shares = by_name(example)["Cached share of input"].measures
    assert [measure.legend for measure in shares] == [pool.name for pool in example.pools]
    for pool, measure in zip(example.pools, shares, strict=True):
        names = metric_names(pool.engine)
        cached, total = (
            rules.over_sessions(rules.awake(pool.name, rules.engine_minute_increase(pool, quantity)))
            for quantity in (Quantity.CACHED_INPUT_TOKENS, Quantity.INPUT_TOKENS)
        )
        assert measure.expression == f"({cached}) / ({total})"
        assert f"{names[Quantity.CACHED_INPUT_TOKENS]}_total" in cached
        assert f"{names[Quantity.INPUT_TOKENS]}_total" in total


def test_the_cold_start_counts_each_promotion_completed_in_the_session_once() -> None:
    (measure,) = objectives.cold_start().measures
    read = selectors(measure.expression)
    promotion = {"transition": stages.Transition.PROMOTION.value}
    # The launches completed from the warm pool, each observed once: a refused or abandoned one is
    # not, and two that took the same seconds are two (tests/unit/lifecycle/test_stages.py).
    assert set(read) == {f"{stages.LAUNCH_METRIC}_count", f"{stages.LAUNCH_METRIC}_bucket", rules.SESSION_ON}
    assert all(one == promotion for one in read[f"{stages.LAUNCH_METRIC}_count"])
    assert all(one == {**promotion, "le": "300"} for one in read[f"{stages.LAUNCH_METRIC}_bucket"])
    # Counted as counters are, each in the minute it was completed: the share of them under 5
    # minutes, and how many there were.
    under = rules.over_sessions(rules.minute_increase(f"{stages.LAUNCH_METRIC}_bucket", {**promotion, "le": "300"}))
    count = rules.over_sessions(rules.minute_increase(f"{stages.LAUNCH_METRIC}_count", promotion))
    assert measure.expression == f"({under}) / ({count})"
    assert measure.count == count
