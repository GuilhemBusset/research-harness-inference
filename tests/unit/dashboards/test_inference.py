"""The inference performance dashboard, built from the example configuration (system design, sections 4.7, 7.2 and 7.3)."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

import pytest

from rhi import config as rhi_config
from rhi_common.engine_metrics import ENGINES, Further, Quantity, further_names, metric_names
from rhi_common.pool_config import ExpectedValues
from rhi_dashboards import build, check, inference, rules
from rhi_gateway import experience

SAMPLES = ("", "_total", "_bucket", "_sum", "_count")


@pytest.fixture
def example() -> rhi_config.Config:
    return rhi_config.load({"RHI_CONFIG": "example"})


def built(config: rhi_config.Config) -> build.Built:
    (one,) = [one for one in build.dashboards(config) if one.module == inference.__name__]
    return one


def rows(config: rhi_config.Config) -> dict[str, dict[str, dict[str, Any]]]:
    """Each row's panels by title, under the title of the row, which is its pool's name."""
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for panel in built(config).document()["panels"]:
        if panel["type"] == "row":
            found[panel["title"]] = {}
        else:
            found[list(found)[-1]][panel["title"]] = panel
    return found


def series(panel: dict[str, Any]) -> dict[str, str]:
    """The panel's queries, each by its legend."""
    return {target["legendFormat"]: target["expr"] for target in panel["targets"]}


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


def test_the_coursework_row_holds_the_seven_panels_with_both_sources(example: rhi_config.Config) -> None:
    coursework = pool(example, "coursework")
    row = rows(example)["coursework"]

    assert list(row) == list(inference.TITLES)
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

    # First token: the engine's, its queue and prefill parts, and the gateway's two waits by start and turn.
    first = expressions[inference.FIRST_TOKEN]
    for quantity in (Quantity.TIME_TO_FIRST_TOKEN, Further.QUEUE_TIME, Further.PREFILL_TIME):
        assert rules.engine_quantile(coursework, 0.95, quantity) in first
        assert rules.engine_request_count(coursework, quantity) in first
    for histogram in (experience.FIRST_TOKEN, experience.FIRST_ANSWER):
        matchers = {"pool": "coursework"}
        assert rules.quantile(0.95, histogram, matchers, by=("start", "turn")) in first
        assert rules.request_count(histogram, matchers, by=("start", "turn")) in first

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

    assert inference.PREFILL not in row
    assert list(row) == [title for title in inference.TITLES if title != inference.PREFILL]
    assert further_names("mock") == {}
    queried = {name for panel in row.values() for e in series(panel).values() for name in names(e)}
    assert not queried & table_names(("vllm",))
    # The transcript panel keeps the gateway's series alone, with no engine series to read asleep.
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
            # The asleep series is zero: it keeps a colour that is neither the flag's red nor its green.
            asleep = [
                {prop["id"]: prop["value"] for prop in override["properties"]}
                for override in panels[inference.CACHED_SHARE]["fieldConfig"]["overrides"]
                if override["matcher"] == {"id": "byFrameRefID", "options": rules.ASLEEP_REF}
            ]
            colours = {one["color"]["fixedColor"] for one in asleep if "color" in one}
            colours |= {step["color"] for one in asleep if "thresholds" in one for step in one["thresholds"]["steps"]}
            assert colours
            assert not colours & {"red", "green"}
            assert all(one["color"]["mode"] == "fixed" for one in asleep if "color" in one)


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
