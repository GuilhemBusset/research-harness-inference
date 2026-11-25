"""The four objectives of section 7.3, each pool's measured while its session is active (`rhi_dashboards.objectives`)."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry

from rhi import config as rhi_config
from rhi_common.boundaries import WAIT_BUCKETS, with_floors
from rhi_common.engine_metrics import Quantity, metric_names
from rhi_dashboards import check, objectives, rules
from rhi_gateway import experience
from rhi_gateway import metrics as gateway
from rhi_gateway.errors import WireFormat
from rhi_scaler import metrics as scaler

SUBQUERY = f"[{rules.SESSION_RANGE}:{rules.SESSION_STEP}]"
LAST_STEP = f"[{rules.SESSION_STEP}:{rules.SESSION_STEP}]"
COLLECTORS = ("services/gateway/collector/config.yaml", "services/engine/collector/config.yaml")
AT_LEAST, ABOVE = objectives.Comparison.AT_LEAST, objectives.Comparison.ABOVE


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


def by_name(config: rhi_config.Config) -> dict[str, objectives.Objective]:
    return {objective.name: objective for objective in objectives.objectives(config)}


def selectors(expression: str) -> dict[str, list[dict[str, str]]]:
    """The metrics the expression reads, each with the matchers of each of its selectors."""
    found: dict[str, list[dict[str, str]]] = {}
    for one in check._read(expression).selectors:
        found.setdefault(one.name, []).append({label: value for label, op, value in one.matchers})
    return found


def ungated(expression: str, session: rules.Session) -> list[str]:
    """The metrics the expression reads otherwise than as what each sample added in the session."""
    read = check._read(expression)
    return [
        one.name
        for one in read.selectors
        if one.name != rules.SESSION_ACTIVE and not check._counted_per_sample(read, one, session)
    ]


def test_the_four_objectives_of_the_design_with_the_example_targets(example: rhi_config.Config) -> None:
    found = by_name(example)
    judged = {
        name: [(m.target, m.percentile, m.comparison, m.threshold) for m in objective.measures]
        for name, objective in found.items()
    }
    # 99.5%; 5 seconds at the 95th percentile, first and later turns apart; 15 and 10 tokens per
    # second, the median and the slowest tenth, on the coursework pool; 85%.
    assert list(judged) == ["Gateway answers", "Time to first token", "Output speed per stream", "Cached share of input"]
    assert judged == {
        "Gateway answers": [(0.995, None, AT_LEAST, 0.995)],
        "Time to first token": [(5, 0.95, AT_LEAST, 0.95)] * 2 * len(example.pools),
        "Output speed per stream": [(15, 0.5, ABOVE, 0.5), (10, 0.1, ABOVE, 0.9)],
        "Cached share of input": [(0.85, None, ABOVE, 0.85)] * len(example.pools),
    }
    # No objective is on the time a launch takes: nothing reads the launches the lifecycle agent observes.
    queries = [query for objective in found.values() for query in objective.queries]
    assert not any("rhi_lifecycle" in query or "launch" in query for query in queries)
    assert not any(isinstance(one, rules.EmittedBy) and "lifecycle" in one.path for one in objectives.METRICS)


def test_a_pools_objective_is_read_in_that_pools_session_and_the_gateways_in_any(example: rhi_config.Config) -> None:
    first = example.pools[0]
    config = dataclasses.replace(example, pools=(*example.pools, dataclasses.replace(first, name="second")))
    found = by_name(config)
    measures = [measure for objective in found.values() for measure in objective.measures]
    of_pools = {name: {measure.pool for measure in objective.measures} for name, objective in found.items()}
    every = {pool.name for pool in config.pools}
    assert of_pools == {
        "Gateway answers": {rules.ANY_POOL},
        "Time to first token": every,
        "Output speed per stream": {"coursework"},
        "Cached share of input": every,
    }
    for measure in measures:
        queries = [query for query in (measure.expression, measure.count) if query]
        for query in queries:
            assert SUBQUERY in query
            read = selectors(query)
            gauge = read.pop(rules.SESSION_ACTIVE)
            assert read
            if measure.pool is rules.ANY_POOL:
                # The gateway's: the session gauge of every pool, and series of no pool.
                assert all(matchers == {} for matchers in gauge)
                assert all("pool" not in matchers for each in read.values() for matchers in each)
            else:
                # A pool's: that pool's gauge, and that pool's series only, so that a request of
                # another pool, or one made in another pool's session, is never counted.
                assert all(matchers == {"pool": measure.pool} for matchers in gauge)
                assert all(matchers["pool"] == measure.pool for each in read.values() for matchers in each)
            # Every series is counted by what each sample added in that session, and only so.
            assert ungated(query, measure.pool) == [], query
            others = [rules.ANY_POOL, *every] if measure.pool else list(every)
            assert all(ungated(query, other) for other in others if other != measure.pool)
    # The gauge the queries read is the one the scaler sends for each pool.
    assert rules.SESSION_ACTIVE == scaler.SESSION_ACTIVE


@pytest.mark.parametrize(("session", "gauge"), [("mock", 'rhi_scaler_session_active{pool="mock"}'), (None, "rhi_scaler_session_active")])
def test_a_sample_counts_by_the_reading_in_force_when_it_was_taken(session: str | None, gauge: str) -> None:
    series = "s"
    # One reading for the session, a pool's or any pool's: the time of the latest, and 1 where any is 1.
    taken = f"max by () (timestamp({gauge}))"
    for previous in rules.PREVIOUS:
        gate = rules.taken_in_session(series, previous, session)
        # The sample's own time against the reading's: the reading at the step if taken no later
        # than the sample, so a scrape a second before the session ends still counts...
        assert f"timestamp(s) >= on () group_left () ({taken} and on () (max by () ({gauge}) == 1))" in gate
        # ...otherwise the one in force before it, so a scrape a second before it begins does not.
        assert f"timestamp(s) < on () group_left () ({taken} and on () (max by () ({previous(gauge)}) == 1))" in gate
        # And never a sample from before the dashboard's range.
        assert f"timestamp(s) > {rules.SESSION_START}" in gate
        assert rules.session_increment(series, previous, session).endswith(f" and {gate})")
    assert rules.SESSION_START == "$__from / 1000"


def test_a_counter_counts_each_sample_once_across_exactly_the_range() -> None:
    (expression,) = objectives.gateway_answers().queries
    # No window of its own: adjacent windows would drop the increment between their scrapes, and one
    # ending at a step would bring in what came before the session.
    assert "increase(" not in expression and "rate(" not in expression
    series = objectives.REQUESTS
    # Each sample less the one a step before, or all of it after a restart.
    step = f"((({series} - {series} offset {rules.SESSION_STEP}) >= 0) or {series})"
    any_pool = rules.ANY_POOL
    assert rules.session_increment(series, rules.a_step_before, any_pool).startswith(f"({step} and ")
    # Summed at every step of exactly the dashboard's range, and from the last step, on a multiple
    # of the step at or before the range's end, to that end.
    steps = f"sum_over_time((sum by () ({rules.session_increment(series, rules.a_step_before, any_pool)})){SUBQUERY})"
    last = f"sum by () ({rules.session_increment(series, rules.at_the_last_step, any_pool)})"
    counted = rules.over_sessions(rules.step_increase(series, session=any_pool))
    assert counted == f"(({steps}) + ({last})) or ({steps}) or ({last})"
    assert rules.at_the_last_step(series) == f"last_over_time({series}{LAST_STEP})"
    assert rules.SESSION_RANGE == "${__range_ms}ms"
    assert counted in expression


def seconds(duration: str) -> int:
    number, unit = re.fullmatch(r"([0-9]+)([sm])", duration).groups()  # ty: ignore[missing-union-attribute] - a test's own duration
    return int(number) * {"s": 1, "m": 60}[unit]


def test_a_step_holds_at_most_one_scrape(example: rhi_config.Config) -> None:
    # Every collector that writes a series the objectives read scrapes less often than a step.
    intervals = [seconds(one) for path in COLLECTORS for one in re.findall(r"scrape_interval: (\S+)", Path(path).read_text())]
    assert intervals and min(intervals) > seconds(rules.SESSION_STEP)
    # And no query reads a counter over a longer span than its step, or over steps other than the
    # range's.
    for objective in objectives.objectives(example):
        for query in (q for measure in objective.measures for q in (measure.expression, measure.count) if q):
            spans = set(re.findall(r"\[[^\]]*\]", query))
            assert spans == {SUBQUERY, LAST_STEP}, query


def test_first_turns_apart_for_each_pool(example: rhi_config.Config) -> None:
    first_token = by_name(example)["Time to first token"]
    assert len(first_token.queries) == 2 * len(example.pools)
    apart = []
    for measure in first_token.measures:
        read = selectors(measure.expression)
        assert set(read) == {f"{experience.FIRST_TOKEN}_bucket", f"{experience.FIRST_TOKEN}_count", rules.SESSION_ACTIVE}
        matchers = read[f"{experience.FIRST_TOKEN}_bucket"][0]
        # The pool's streamed requests under the target's boundary, whatever else the gateway says of them...
        assert re.fullmatch(matchers.pop("le"), "5")
        assert "start" not in matchers
        apart.append((matchers["pool"], matchers["turn"]))
        # ...over all of the same requests, which the panel shows beside it.
        assert read[f"{experience.FIRST_TOKEN}_count"][0] == matchers
        assert selectors(measure.count or "")[f"{experience.FIRST_TOKEN}_count"][0] == matchers
    assert apart == [(pool.name, turn) for pool in example.pools for turn in ("first", "later")]


def test_no_objective_selects_how_a_request_started(example: rhi_config.Config) -> None:
    # The gateway's histograms no longer tell a held request from a warm one: a query that selected
    # that label would read nothing.
    read = [selectors(query) for objective in objectives.objectives(example) for query in objective.queries]
    assert read and not [matchers for each in read for found in each.values() for matchers in found if "start" in matchers]


def test_a_stream_the_gateway_records_counts_in_its_pools_objectives(example: rhi_config.Config) -> None:
    # The gateway records a first turn as it records every stream: its first token after 6 seconds,
    # then 4 tokens a second.
    registry = CollectorRegistry()
    made = experience.Experience(registry, clock=iter((0.0, 6.0, 7.0)).__next__)
    turn = made.request("coursework", WireFormat.MESSAGES, first_turn=True)
    turn.token_sent(answer=True)
    turn.token_sent(answer=True)
    turn.settled(input_tokens=20, output_tokens=5)
    served = {sample.name: sample for family in registry.collect() for sample in family.samples}
    found = by_name(example)
    # It is among the observations of its pool's first turns, and of both measures of the speed.
    for family, name, measured in ((experience.FIRST_TOKEN, "Time to first token", 1), (experience.SPEED, "Output speed per stream", 2)):
        sample = served[f"{family}_count"]
        assert sample.value == 1 and "start" not in sample.labels
        counting = [
            measure.pool
            for measure in found[name].measures
            if all(
                sample.labels[label] == value
                for matchers in selectors(measure.count or "")[f"{family}_count"]
                for label, value in matchers.items()
            )
        ]
        assert counting == ["coursework"] * measured


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
    assert targets["Time to first token"] == [4, 4] * len(example.pools)
    assert targets["Output speed per stream"] == [20, 12.5]
    assert targets["Cached share of input"] == [0.9] * len(example.pools)
    # The design's own is not the configuration's.
    assert targets["Gateway answers"] == [0.995]
    # And the shares are read at the configured boundaries.
    speed = found["Output speed per stream"].queries
    assert [nearest(query, f"{experience.SPEED}_bucket") for query in speed] == ["20", "12.5"]
    first_token = found["Time to first token"].queries[0]
    assert nearest(first_token, f"{experience.FIRST_TOKEN}_bucket") == "4"
    # The panel says the target it applies, as configured.
    (cached, *_) = found["Cached share of input"].measures
    assert "above 90% " in cached.goal
    assert "above 85.4% " in objectives.cached_share(dataclasses.replace(other, cached_share_flag_percent=85.4)).measures[0].goal


def bounds(expression: str, bucket: str) -> list[str]:
    """The boundaries the expression selects of the bucket series, as the workspace spells them."""
    (pattern,) = {one["le"] for one in selectors(expression)[bucket]}
    return pattern.replace("\\", "").split("|")


def nearest(expression: str, bucket: str) -> str:
    """The boundary of a series that counts the floor, which the expression reads it at: the
    largest it selects for a wait (`max`), the smallest finite one for a speed (`min`)."""
    finite = [one for one in bounds(expression, bucket) if one != "+Inf"]
    return max(finite, key=float) if "max without (le) (" in expression else min(finite, key=float)


def under(expression: str, bucket: str, buckets: Mapping[str, float], reduce: Callable[[list[float]], float]) -> float:
    """What the expression reads of one series' cumulative buckets: those it selects, reduced."""
    patterns = {one["le"] for one in selectors(expression)[bucket]}
    selected = [count for le, count in buckets.items() if all(re.fullmatch(p, le) for p in patterns)]
    return reduce(selected)


def read(expression: str, bucket: str, series: Sequence[Mapping[str, float]]) -> float:
    """The share a bound's query reads of each series' cumulative buckets: the `max` or `min` of
    those it selects, none where it selects none, summed over all their observations; the share at
    the floor or more, for a speed."""
    reduce = max if "max without (le) (" in expression else min
    below = sum(under(expression, bucket, one, lambda selected: reduce(selected) if selected else 0) for one in series)
    total = sum(one["+Inf"] for one in series)
    return (total - below) / total if bucket.startswith(experience.SPEED) else below / total


def shown(measure: objectives.Measure, least: float, most: float) -> float:
    """What the measure's query shows for the bounds of its share (`Measure.expression`)."""
    return least if measure.met(least) else most if not measure.met(most) else objectives.INCONCLUSIVE


def test_a_revised_floor_is_read_exactly_at_a_boundary_the_gateway_counts(tmp_path: Path) -> None:
    # Floors the configuration takes, between the gateway's own boundaries.
    text = Path("config.example.toml").read_text(encoding="utf-8")
    for key, value in (("target_ttft_p95_seconds", "6"), ("target_stream_median_tokens_per_second", "16")):
        text = re.sub(rf"(?m)^{key} = .*$", f"{key} = {value}", text)
    (tmp_path / "revised.toml").write_text(text, encoding="utf-8")
    config = rhi_config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(tmp_path / "revised.toml")})
    first, *_ = objectives.first_token(config).measures
    median, _ = objectives.stream_speed(config).measures
    wait = f"{experience.FIRST_TOKEN}_bucket"
    for measure, bucket, floor in ((first, wait, "6"), (median, f"{experience.SPEED}_bucket", "16")):
        assert {nearest(measure.share, bucket), nearest(measure.ceiling or "", bucket)} == {floor}
    # The gateway counts its waits with the floor among its boundaries (`rhi_gateway.settings`): 95
    # of 100 first tokens under 6 seconds meet the objective, 90 do not, and both bounds agree.
    wait_buckets = with_floors(WAIT_BUCKETS, config.target_ttft_p95_seconds)
    labels = {"pool": "coursework", "wire_format": "messages", "turn": "first"}
    for waits, met in (([4.0] * 90 + [5.5] * 5 + [7.0] * 5, True), ([4.0] * 90 + [7.0] * 10, False)):
        registry = CollectorRegistry()
        histogram = experience.Experience(registry, wait_buckets=wait_buckets).first_token
        for wait_seconds in waits:
            histogram.labels(*labels.values()).observe(wait_seconds)
        served = {
            objectives.boundary(float(le)) if le != objectives.INFINITY else le: sample.value
            for family in registry.collect()
            for sample in family.samples
            if sample.name == wait and (le := sample.labels["le"])
        }
        least, most = read(first.share, wait, [served]), read(first.ceiling or "", wait, [served])
        assert least == most
        assert first.met(shown(first, least, most)) is met


def test_older_buckets_settle_a_revised_floor_only_where_they_prove_it(example: rhi_config.Config) -> None:
    config = dataclasses.replace(example, target_ttft_p95_seconds=6, target_stream_median_tokens_per_second=12)
    first, *_ = objectives.first_token(config).measures
    median, _ = objectives.stream_speed(config).measures
    wait, speed = f"{experience.FIRST_TOKEN}_bucket", f"{experience.SPEED}_bucket"
    # The least share where it meets the threshold, the most where it misses, else inconclusive.
    for measure, meets, misses in ((first, ">=", "<"), (median, ">", "<=")):
        threshold = repr(measure.threshold)
        assert measure.expression == (
            f"(({measure.share}) {meets} {threshold}) or (({measure.ceiling}) {misses} {threshold})"
            f" or ((0 * (({measure.count}) > 0)) - 1)"
        )
        assert measure.met(objectives.INCONCLUSIVE) is None
    # 100 first turns at 5.5 seconds, from a task with no bucket at 6: between none and all of them
    # under 6, so neither met nor missed.
    straddling = {"4": 0, "5": 0, "7.5": 100, "10": 100, "+Inf": 100}
    assert (read(first.share, wait, [straddling]), read(first.ceiling or "", wait, [straddling])) == (0, 1)
    assert shown(first, 0, 1) == objectives.INCONCLUSIVE
    # Waits under 5 prove it met, and one newer wait under 6 makes 101 of 101; waits of 7.5 or more
    # prove it missed.
    older, newer = {"5": 100, "7.5": 100, "+Inf": 100}, {"5": 0, "6": 1, "7.5": 1, "+Inf": 1}
    assert read(first.share, wait, [older, newer]) == 1
    late = {"5": 0, "7.5": 0, "+Inf": 100}
    assert read(first.ceiling or "", wait, [late]) == 0 and shown(first, 0, 0) == 0
    # A speed: an older task's 5 streams under 12.5 may all be under 12, its 2 under 10 surely are,
    # so between half and 80% are at 12 or more: neither above half proven, nor ruled out.
    older = {"10": 2, "12.5": 5, "+Inf": 10}
    least, most = read(median.share, speed, [older]), read(median.ceiling or "", speed, [older])
    assert (least, most) == (0.5, 0.8)
    assert shown(median, least, most) == objectives.INCONCLUSIVE
    # A newer task counts the floor, and its bounds agree; one with no finite boundary over the floor
    # is bounded by all its streams.
    newer = {"10": 1, "12": 4, "12.5": 4, "+Inf": 10}
    assert read(median.share, speed, [newer]) == read(median.ceiling or "", speed, [newer]) == 0.6
    assert read(median.share, speed, [{"10": 2, "+Inf": 10}]) == 0


def test_a_percentile_is_judged_exactly_at_its_floor(example: rhi_config.Config) -> None:
    found = by_name(example)
    first, *_ = found["Time to first token"].measures
    median, tenth = found["Output speed per stream"].measures
    # Every first token at exactly 5 seconds: none under 5, so the 95th percentile is not under 5.
    assert not first.met(0.0)
    assert first.met(0.95) and not first.met(0.9499)
    # Every stream at exactly 15 tokens per second: all at 15 or more, so the median is.
    assert median.met(1.0)
    # Half the streams under 15: the median, the lower of the two middle ones, is under it.
    assert not median.met(0.5)
    assert tenth.met(0.91) and not tenth.met(0.9)


def test_a_boundary_is_selected_as_the_collector_writes_it() -> None:
    # Go's FormatFloat(bound, 'f', -1, 64): no ".0", no exponent, the fewest digits.
    assert [objectives.boundary(value) for value in (5, 5.0, 15, 12.5, 0.25, 300, 1800, 1e-05, 1e16)] == [
        "5", "5", "15", "12.5", "0.25", "300", "1800", "0.00001", "10000000000000000"
    ]


def test_the_example_floors_are_boundaries_the_gateway_counts_strictly_under(example: rhi_config.Config) -> None:
    registry = CollectorRegistry()
    made = experience.Experience(registry)
    made.speed.labels("coursework", "messages").observe(1)
    made.first_token.labels("coursework", "messages", "first").observe(1)
    # The boundaries the shares read are the gateway's, in the workspace's spelling of them.
    served = {
        (sample.name, objectives.boundary(float(sample.labels["le"])))
        for family in registry.collect()
        for sample in family.samples
        if "le" in sample.labels and sample.labels["le"] != "+Inf"
    }
    selected = set()
    for objective in by_name(example).values():
        for measure in objective.measures:
            for name in selectors(measure.expression):
                if name.endswith("_bucket"):
                    selected.add((name, nearest(measure.expression, name)))
    # 5 seconds, and 15 and 10 tokens a second.
    assert len(selected) == 3
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
    assert set(read) == {gateway.OWN_FAULTS + "_total", gateway.REQUESTS + "_total", rules.SESSION_ACTIVE}
    # Summed whatever their labels: no pool, no student.
    assert all(matchers == {} for name, each in read.items() if name != rules.SESSION_ACTIVE for matchers in each)
    assert "sum by () (" in measure.expression
    faults = rules.over_sessions(rules.step_increase(objectives.OWN_FAULTS, session=rules.ANY_POOL))
    requests = rules.over_sessions(rules.step_increase(objectives.REQUESTS, session=rules.ANY_POOL))
    # Where no fault was counted, zero faults are read: the share of a session without one is 1.
    assert measure.expression == f"(({requests}) - (({faults}) or on () (0 * ({requests})))) / ({requests})"


def test_the_speed_is_measured_on_the_coursework_pool_alone(example: rhi_config.Config) -> None:
    coursework = next(pool for pool in example.pools if pool.name == "coursework")
    # Another GPU pool has floors of its own: the objective is not measured on it, and it stays on
    # the coursework pool when the gateway's default route points at the other.
    upgrade = dataclasses.replace(coursework, name="upgrade")
    for default_pool in ("coursework", "upgrade"):
        config = dataclasses.replace(example, pools=(*example.pools, upgrade), default_pool=default_pool)
        speed = by_name(config)["Output speed per stream"]
        assert [measure.pool for measure in speed.measures] == ["coursework"] * 2
        read = [selectors(query)[f"{experience.SPEED}_count"][0] for query in speed.queries]
        assert read == [{"pool": "coursework"}] * 2
    # A configuration with no coursework pool has no measure of speed.
    without = dataclasses.replace(example, pools=tuple(pool for pool in example.pools if pool.name != "coursework"))
    assert by_name(without)["Output speed per stream"].measures == ()


def test_the_cached_share_counts_every_engine_sample_in_the_session(example: rhi_config.Config) -> None:
    shares = by_name(example)["Cached share of input"].measures
    assert [measure.legend for measure in shares] == [pool.name for pool in example.pools]
    for pool, measure in zip(example.pools, shares, strict=True):
        names = metric_names(pool.engine)
        cached, total = (
            rules.over_sessions(rules.engine_step_increase(pool, quantity))
            for quantity in (Quantity.CACHED_INPUT_TOKENS, Quantity.INPUT_TOKENS)
        )
        assert measure.expression == f"({cached}) / ({total})"
        assert f"{names[Quantity.CACHED_INPUT_TOKENS]}_total" in cached
        assert f"{names[Quantity.INPUT_TOKENS]}_total" in total
        # Not kept to the scaler's count of instances in service, which may lag behind an instance
        # that has just started reporting: a series no instance updates adds nothing anyway.
        assert rules.IN_SERVICE not in measure.expression
