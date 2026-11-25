"""The inference performance dashboard, built from the example configuration (system design, sections 4.7, 7.2 and 7.3)."""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

import pytest

from rhi import config as rhi_config
from rhi_common.engine_metrics import ENGINES, Further, Quantity, further_names, metric_names
from rhi_common.pool_config import ExpectedValues
from rhi_dashboards import build, check, cost, gpu, inference, rules
from rhi_gateway import experience
from rhi_gateway import metrics as gateway_metrics

SAMPLES = ("", "_total", "_bucket", "_sum", "_count")


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


def built(config: rhi_config.Config) -> build.Built:
    (one,) = [one for one in build.dashboards(config) if one.module == inference.__name__]
    return one


def rows(config: rhi_config.Config, uid: str = inference.UID) -> dict[str, dict[str, dict[str, Any]]]:
    """Each row's panels by title, under the title of the row, which is its pool's name."""
    found: dict[str, dict[str, dict[str, Any]]] = {}
    (document,) = [one.document() for one in build.dashboards(config) if one.uid == uid]
    for panel in document["panels"]:
        if panel["type"] == "row":
            found[panel["title"]] = {}
        else:
            found[list(found)[-1]][panel["title"]] = panel
    return found


def series(panel: dict[str, Any]) -> dict[str, str]:
    """The panel's Prometheus queries, each by its legend."""
    return {target["legendFormat"]: target["expr"] for target in panel["targets"] if "expr" in target}


def units(panel: dict[str, Any]) -> dict[str, str]:
    """The unit of each query read on its own axis, by the query's legend."""
    legends = {target["refId"]: target.get("legendFormat") for target in panel["targets"]}
    return {
        legends[override["matcher"]["options"]]: prop["value"]
        for override in panel["fieldConfig"]["overrides"]
        for prop in override["properties"]
        if prop["id"] == "unit"
    }


def names(expression: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*:[A-Za-z0-9_:]+", expression))


def table_names(engines: tuple[str, ...]) -> set[str]:
    """Every series name the per-engine table gives the engines, with each sample's suffix."""
    families = {
        str(name) for engine in engines for name in (*metric_names(engine).values(), *further_names(engine).values())
    }
    return {family + sample for family in families for sample in SAMPLES}


def pool(config: rhi_config.Config, name: str) -> Any:
    return next(one for one in config.pools if one.name == name)


def test_each_pool_has_its_row_and_the_engine_queries_follow_the_rules(example: rhi_config.Config) -> None:
    dashboard = built(example)
    found = rows(example)

    assert {one.engine for one in example.pools} == {"mock", "vllm"}
    assert list(found) == [one.name for one in example.pools]
    assert check.violations(dashboard.document(), dashboard.metrics, example.pools) == []
    # Every engine name a row queries is one the table gives at the row of that pool's engine.
    every_engine = table_names(ENGINES)
    for one in example.pools:
        queried = {name for panel in found[one.name].values() for e in series(panel).values() for name in names(e)}
        assert queried & every_engine <= table_names((one.engine,)), one.name
        assert queried & every_engine
        # A panel that draws a series of the engine or of an instance reads the pool's state
        # while the pool does not serve: "no session", or "waiting for capacity".
        states = list(rules.state_queries(one.name).values())
        kept = rules.serving(one.name, "")[len("(") :]
        of_instances = [
            panel for panel in found[one.name].values() if any(e.endswith(kept) for e in series(panel).values())
        ]
        assert of_instances
        for panel in of_instances:
            assert [e for e in series(panel).values() if e in states] == states, panel["title"]


def test_the_coursework_row_holds_every_panel_with_both_sources(example: rhi_config.Config) -> None:
    coursework = pool(example, "coursework")
    row = rows(example)["coursework"]

    assert list(row) == list(inference.TITLES)
    # The six panels of cache, load and cost follow the seven of speed and latency, one for each.
    new = (inference.CACHE, inference.FORGOT, inference.STREAMS, inference.STUDENTS, inference.COST, inference.PER_GPU)
    assert list(row)[7:] == list(new)
    expressions = {title: " ".join(series(panel).values()) for title, panel in row.items()}
    computed = rules.engine_selector(coursework, Further.COMPUTED_INPUT_TOKENS)
    assert 'source="local_compute"' in computed

    # Input: the engine's input rate, the part computed beside it, and the gateway's settled input.
    assert rules.engine_rate(coursework, Quantity.INPUT_TOKENS) in expressions[inference.INPUT]
    assert rules.engine_rate(coursework, Further.COMPUTED_INPUT_TOKENS) in expressions[inference.INPUT]
    assert rules.rate(f"{experience.INPUT_TOKENS}_sum", {"pool": "coursework"}) in expressions[inference.INPUT]

    # Speed: the mean over decode time, the time per output token, the gateway's histogram, and
    # each instance's computed input.
    speed = expressions[inference.SPEED]
    assert rules.engine_series(coursework, Further.DECODE_TIME, "_sum") in speed
    assert rules.engine_series(coursework, Quantity.TIME_PER_OUTPUT_TOKEN, "_bucket") in speed
    assert f"{experience.SPEED}_bucket" in speed
    assert rules.engine_rate(coursework, Further.COMPUTED_INPUT_TOKENS, by=("instance",)) in speed

    # First token: the engine's, its queue and prefill parts, and the gateway's two waits by turn alone.
    first = expressions[inference.FIRST_TOKEN]
    for quantity in (Quantity.TIME_TO_FIRST_TOKEN, Further.QUEUE_TIME, Further.PREFILL_TIME):
        assert rules.engine_quantile(coursework, 0.95, quantity) in first
        assert rules.engine_request_count(coursework, quantity) in first
    for histogram in (experience.FIRST_TOKEN, experience.FIRST_ANSWER):
        matchers = {"pool": "coursework"}
        assert rules.quantile(0.95, histogram, matchers, by=("turn",)) in first
        assert rules.request_count(histogram, matchers, by=("turn",)) in first

    # Throughput, per pool and per instance, and transcript sizes, from both sources.
    assert rules.engine_rate(coursework, Quantity.OUTPUT_TOKENS, by=("instance",)) in expressions[inference.THROUGHPUT]
    assert f"{experience.OUTPUT_TOKENS}_sum" in expressions[inference.THROUGHPUT]
    for family in (experience.INPUT_TOKENS, experience.OUTPUT_TOKENS):
        assert f"{family}_bucket" in expressions[inference.TRANSCRIPT]
    assert rules.engine_series(coursework, Further.REQUEST_INPUT_TOKENS, "_bucket") in expressions[inference.TRANSCRIPT]
    assert (
        rules.engine_series(coursework, Further.REQUEST_PREFILL_COMPUTED_TOKENS, "_sum")
        in expressions[inference.PREFILL]
    )


def test_the_mock_row_draws_nothing_its_engine_leaves_out(example: rhi_config.Config) -> None:
    row = rows(example)["mock"]

    left_out = (inference.PREFILL, inference.FORGOT, inference.PER_GPU)
    assert list(row) == [title for title in inference.TITLES if title not in left_out]
    assert len(row) == len(rows(example)["coursework"]) - len(left_out)
    # The cache panel: its use and the reported capacity, no preemptions.
    assert {legend.split(" {{")[0] for legend in series(row[inference.CACHE])} == {
        "cache use",
        "reported capacity",
        *(state.text for state in rules.STATES),
    }
    assert further_names("mock") == {}
    queried = {name for panel in row.values() for e in series(panel).values() for name in names(e)}
    assert not queried & table_names(("vllm",))
    # The transcript panel keeps the gateway's series alone, with no engine series to read the pool's state for.
    transcript = series(row[inference.TRANSCRIPT])
    assert transcript
    assert all("rhi_gateway_" in expression for expression in transcript.values())


def test_the_cached_share_is_flagged_below_the_configured_threshold(example: rhi_config.Config) -> None:
    for config, threshold in ((example, 0.85), (dataclasses.replace(example, cached_share_flag_percent=70), 0.7)):
        for panels in rows(config).values():
            defaults = panels[inference.CACHED_SHARE]["fieldConfig"]["defaults"]
            steps = defaults["thresholds"]["steps"]
            assert [step["color"] for step in steps] == ["red", "green"]
            assert steps[1]["value"] == pytest.approx(threshold)
            assert defaults["color"]["mode"] == "thresholds"
            assert defaults["custom"]["thresholdsStyle"]["mode"] != "off"
            # A state's series is zero: each keeps a colour that is neither the flag's red nor its green.
            for state in rules.STATES:
                of_state = [
                    {prop["id"]: prop["value"] for prop in override["properties"]}
                    for override in panels[inference.CACHED_SHARE]["fieldConfig"]["overrides"]
                    if override["matcher"] == {"id": "byFrameRefID", "options": state.ref}
                ]
                colours = {one["color"]["fixedColor"] for one in of_state if "color" in one}
                colours |= {
                    step["color"] for one in of_state if "thresholds" in one for step in one["thresholds"]["steps"]
                }
                assert colours
                assert not colours & {"red", "green"}
                assert all(one["color"]["mode"] == "fixed" for one in of_state if "color" in one)


def lines(panel: dict[str, Any], prefix: str) -> list[str]:
    return sorted(expression for legend, expression in series(panel).items() if legend.startswith(prefix))


def test_the_prediction_is_on_the_panel(example: rhi_config.Config) -> None:
    coursework = pool(example, "coursework")
    assert coursework.expected is not None
    expected = dataclasses.replace(
        coursework.expected, stream_tokens_per_second=(11.12501, 27), time_to_first_token_seconds=(0.7, 2.250001)
    )
    changed = tuple(dataclasses.replace(one, expected=expected) if one is coursework else one for one in example.pools)
    config = dataclasses.replace(
        example, pools=changed, target_ttft_p95_seconds=4.125001, target_stream_p10_tokens_per_second=9
    )
    found = rows(config)

    # The values are drawn as configured, never rounded.
    speed, first = found["coursework"][inference.SPEED], found["coursework"][inference.FIRST_TOKEN]
    assert lines(speed, inference.EXPECTED) == ["vector(11.12501)", "vector(27)"]
    assert lines(first, inference.EXPECTED) == ["vector(0.7)", "vector(2.250001)"]
    assert lines(speed, "floor") == ["vector(15)", "vector(9)"]
    assert lines(first, "floor") == ["vector(4.125001)"]

    # The mock pool gives no expected values: its panels carry the floors and no expected line.
    assert pool(config, "mock").expected is None
    for panel in found["mock"].values():
        assert lines(panel, inference.EXPECTED) == []
    assert lines(found["mock"][inference.SPEED], "floor") == ["vector(15)", "vector(9)"]
    assert lines(found["mock"][inference.FIRST_TOKEN], "floor") == ["vector(4.125001)"]
    # A pool with expected values that leave one out shows only the floors on that panel.
    partial = ExpectedValues(stream_tokens_per_second=None, time_to_first_token_seconds=(1, 2), cache_tokens=None)
    one = dataclasses.replace(coursework, expected=partial)
    only = rows(dataclasses.replace(example, pools=(one,)))["coursework"]
    assert lines(only[inference.SPEED], inference.EXPECTED) == []
    assert lines(only[inference.FIRST_TOKEN], inference.EXPECTED) == ["vector(1)", "vector(2)"]
    # With no expected cache capacity, the cache panel shows the reported capacity alone.
    for cache in (only[inference.CACHE], found["mock"][inference.CACHE]):
        assert lines(cache, inference.EXPECTED) == []
        assert inference.REPORTED_CAPACITY in series(cache)


def test_the_new_panels_read_their_sources(example: rhi_config.Config) -> None:
    coursework = pool(example, "coursework")
    row = rows(example)["coursework"]
    expressions = {title: " ".join(series(panel).values()) for title, panel in row.items()}

    # Cache: each instance's use and preemptions, and the reported capacity beside the expected
    # bounds of the example configuration, on the reported capacity's axis.
    cache = row[inference.CACHE]
    assert f"by (pool, instance) ({rules.engine_selector(coursework, Quantity.CACHE_USAGE)})" in expressions[inference.CACHE]
    assert rules.engine_rate(coursework, Further.PREEMPTIONS, by=("instance",)) in expressions[inference.CACHE]
    assert f"{inference.CACHE_CAPACITY}{{pool=\"coursework\"}}" in expressions[inference.CACHE]
    assert coursework.expected is not None
    assert coursework.expected.cache_tokens is not None
    assert lines(cache, inference.EXPECTED) == [f"vector({int(bound)})" for bound in coursework.expected.cache_tokens]
    axes = units(cache)
    assert axes[inference.EXPECTED_LOW] == axes[inference.EXPECTED_HIGH] == axes[inference.REPORTED_CAPACITY]

    # The sessions the cache forgot: the engine's idle time before eviction, with the blocks it observed.
    forgot = series(row[inference.FORGOT])
    assert rules.engine_quantile(coursework, 0.95, Further.BLOCK_IDLE_BEFORE_EVICTION) in " ".join(forgot.values())
    count = rules.engine_request_count(coursework, Further.BLOCK_IDLE_BEFORE_EVICTION)
    assert count in forgot[inference.EVICTED_BLOCKS]
    assert not [legend for legend in forgot if "request" in legend]

    # Streams: each instance's running and waiting requests, and the gateway's in flight.
    for quantity in (Quantity.RUNNING_REQUESTS, Quantity.WAITING_REQUESTS):
        assert f"by (pool, instance) ({rules.engine_selector(coursework, quantity)})" in expressions[inference.STREAMS]
    assert f"sum by (pool) ({experience.IN_FLIGHT}{{pool=\"coursework\"}})" in expressions[inference.STREAMS]
    # The gateway holds no request: its one gauge on the panel is the requests in flight, and no
    # panel of any dashboard reads a gauge of requests held.
    assert [legend for legend in series(row[inference.STREAMS]) if legend.startswith("gateway")] == ["gateway in flight"]
    assert not [one.uid for one in build.dashboards(example) if "requests_held" in json.dumps(one.document())]

    # Students over the instances in service, an average by its title.
    assert "average" in inference.STUDENTS
    assert f"{gateway_metrics.REQUESTS}_total" in expressions[inference.STUDENTS]
    assert rules.in_service("coursework") in expressions[inference.STUDENTS]

    # Tokens per GPU-hour and per joule: the output rate, the instances in service, the energy's series.
    per_gpu = expressions[inference.PER_GPU]
    assert rules.engine_rate(coursework, Quantity.OUTPUT_TOKENS) in per_gpu
    assert rules.in_service("coursework") in per_gpu
    assert rules.rate(gpu.ENERGY_SERIES, {"pool": "coursework"}) in per_gpu

    # Cost: one panel, the cost dashboard's own computation over the dashboard's range, whatever its
    # dates, linked to the month's estimate on the cost dashboard.
    panel = row[inference.COST]
    assert "estimate" in panel["title"].lower()
    (over_range,) = rows(example, cost.SESSION_UID)["coursework"].values()
    assert panel["targets"] == over_range["targets"]
    assert "timeFrom" not in panel
    assert [link["url"] for link in panel["links"]] == [f"/d/{cost.UID}"]
    assert built(example).document()["timezone"] == cost.TIME_ZONE


def test_the_gpu_panel_is_only_for_a_pool_with_a_gpu(example: rhi_config.Config) -> None:
    found = rows(example)
    for one in example.pools:
        assert (inference.PER_GPU in found[one.name]) is (one.engine != "mock"), one.name


# A series of the per-student counter: its labels, and its samples as (seconds, value).
type Reported = list[tuple[dict[str, str], list[tuple[int, float]]]]
LOOKBACK = 300


def last_over(samples: list[tuple[int, float]], at: int) -> float | None:
    """`last_over_time` over five minutes ending at a time: the last sample in the window, read
    from the samples themselves, so a series that went stale since still has it."""
    found = [value for time, value in samples if at - LOOKBACK < time <= at]
    return found[-1] if found else None


def students_at(example: rhi_config.Config, reported: Reported, at: int, in_service: int) -> dict[tuple[str, ...], float]:
    """The students-per-instance query of the coursework pool, evaluated on the series reported."""
    coursework = pool(example, "coursework")
    query = series(rows(example)["coursework"][inference.STUDENTS])["students per instance"]
    assert inference.students(coursework) in query
    assert rules.in_service("coursework") in query
    shape = re.fullmatch(
        r"\(count by \((?P<outer>[^)]*)\) \(count by \((?P<inner>[^)]*)\) \((?P<active>.*)\)\)\) or \(0 \* (?P<zero>.*)\)",
        inference.students(coursework),
    )
    assert shape is not None
    assert shape["zero"] == f"sum by ({shape['outer']}) ({rules.in_service('coursework')})"
    outer, inner = (tuple(label.strip() for label in shape[part].split(",")) for part in ("outer", "inner"))
    # A series counted a request when its last sample in the window differs from its last one in
    # the window before, or it reported none then.
    now = r"last_over_time\((?P=s)\[5m\]\)"
    before = r"last_over_time\((?P=s)\[5m\] offset 5m\)"
    active = re.fullmatch(
        rf"\(last_over_time\((?P<s>\w+\{{[^}}]*\}})\[5m\]\) != {before}\) or \({now} unless {before}\)", shape["active"]
    )
    assert active is not None
    (pattern,) = re.findall(r'alias=~"([^"]*)"', active["s"])
    assert active["s"].startswith(f"{gateway_metrics.REQUESTS}_total{{")

    students: set[tuple[str, ...]] = set()
    for labels, samples in reported:
        now, before = last_over(samples, at), last_over(samples, at - LOOKBACK)
        if now is not None and now != before and re.fullmatch(pattern, labels.get("alias", "")):
            students.add(tuple(labels.get(label, "") for label in inner))
    counted: dict[tuple[str, ...], float] = {}
    for student in students:
        key = tuple(dict(zip(inner, student, strict=True))[label] for label in outer)
        counted[key] = counted.get(key, 0) + 1
    if not counted:
        counted = {("coursework",): 0}
    return {key: value / in_service for key, value in counted.items()} if in_service else {}


def test_a_student_counts_once(example: rhi_config.Config) -> None:
    # One alias whose requests two gateway tasks reported, beside a request of no student.
    rose = [(0, 1.0), *((time, 2.0) for time in range(15, 301, 15))]
    one_student: Reported = [
        ({"pool": "coursework", "alias": "otter", "instance": "task-a"}, rose),
        ({"pool": "coursework", "alias": "otter", "instance": "task-b"}, rose),
        ({"pool": "coursework", "alias": "", "instance": "task-a"}, rose),
    ]
    assert students_at(example, one_student, 301, in_service=1) == {("coursework",): 1}
    # A series that appeared in the window holds its first request; a second alias is a second student.
    heron = ({"pool": "coursework", "alias": "heron", "instance": "task-b"}, [(200, 1.0), (215, 1.0)])
    assert students_at(example, [*one_student, heron], 301, in_service=1) == {("coursework",): 2}


def test_recent_students_survive_a_stale_gateway_series(example: rhi_config.Config) -> None:
    # A request scraped at 15 s, after which the task stopped reporting: still in the window at 300 s.
    gone = ({"pool": "coursework", "alias": "otter", "instance": "task-a"}, [(0, 1.0), (15, 2.0)])
    assert students_at(example, [gone], 300, in_service=1) == {("coursework",): 1}
    # A task that missed the scrapes around the window's start and came back unchanged counted nothing.
    gap = [*((time, 2.0) for time in range(-300, -15, 15)), *((time, 2.0) for time in range(15, 301, 15))]
    unchanged = ({"pool": "coursework", "alias": "otter", "instance": "task-a"}, gap)
    assert students_at(example, [unchanged], 300, in_service=1) == {("coursework",): 0}


def test_an_idle_pool_has_no_student_per_instance(example: rhi_config.Config) -> None:
    flat = [(time, 2.0) for time in range(0, 301, 15)]
    idle: Reported = [({"pool": "coursework", "alias": "otter", "instance": instance}, flat) for instance in "ab"]
    assert students_at(example, idle, 301, in_service=1) == {("coursework",): 0}
    assert students_at(example, idle, 301, in_service=0) == {}
