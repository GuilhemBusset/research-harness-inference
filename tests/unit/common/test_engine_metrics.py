"""The per-engine table of metric names (`rhi_common.engine_metrics`; system design, sections 4.1
and 7.2)."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from rhi_common.engine_metrics import (
    ENGINES,
    LABEL,
    MOCK,
    NAME,
    VLLM,
    Further,
    Kind,
    Name,
    Quantity,
    cache_capacity,
    further_names,
    metric_names,
)

DESIGN = Path(__file__).resolve().parents[3] / "docs" / "design" / "system-design.md"


def quantity_names(engine: str) -> list[Name]:
    """The names of the row's quantities: the contract's, then the further ones the engine has."""
    return [*metric_names(engine).values(), *further_names(engine).values()]


def every_name(engine: str) -> list[Name]:
    """Every name of the engine's row: those of its quantities, then the information metric that
    carries its cache capacity."""
    place = cache_capacity(engine)
    return [*quantity_names(engine), *([] if place is None else [place.metric])]


def test_the_contract_row_has_its_eight_quantities() -> None:
    assert {quantity.value for quantity in Quantity} == {
        "running_requests",
        "waiting_requests",
        "time_to_first_token",
        "time_per_output_token",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "cache_usage",
    }
    assert Quantity.RUNNING_REQUESTS.kind is Kind.GAUGE
    assert Quantity.TIME_TO_FIRST_TOKEN.kind is Kind.HISTOGRAM
    assert Quantity.OUTPUT_TOKENS.kind is Kind.COUNTER


@pytest.mark.parametrize("engine", ENGINES)
def test_each_row_names_every_quantity_once(engine: str) -> None:
    row = metric_names(engine)

    assert set(row) == set(Quantity)
    names = every_name(engine)
    assert len(set(names)) == len(names)
    assert all(NAME.fullmatch(name) for name in names)  # which no empty name does
    # The type the contract gives the quantity is the one its name says.
    assert all(row[quantity].kind is quantity.kind for quantity in Quantity)


@pytest.mark.parametrize("engine", ENGINES)
def test_each_name_says_its_type_and_the_labels_that_select_its_part(engine: str) -> None:
    for name in every_name(engine):
        assert isinstance(name, Name)
        assert isinstance(name.kind, Kind)
        assert all(LABEL.fullmatch(label) and value for label, value in name.labels.items())
        # A counter's family name never carries the suffix of its sample.
        assert not (name.kind is Kind.COUNTER and name.endswith("_total"))
        # A name is a string, and a copy of it still says the same.
        copied = copy.deepcopy(name)
        assert (copied, copied.kind, copied.labels) == (str(name), name.kind, name.labels)


@pytest.mark.parametrize("engine", ENGINES)
def test_each_row_says_where_the_cache_capacity_is_a_metric_of_its_own_and_a_label(engine: str) -> None:
    place = cache_capacity(engine)

    assert place is not None
    assert NAME.fullmatch(place.metric)
    assert LABEL.fullmatch(place.label)
    # An information metric is a gauge whose value is always 1, and the label to read selects no part of it.
    assert (place.metric.kind, place.metric.labels) == (Kind.GAUGE, {})
    # It stands beside the row's quantities, and is not one of them.
    assert place.metric not in quantity_names(engine)


def test_the_vllm_row_names_the_further_quantities_and_the_mock_leaves_them_out() -> None:
    assert set(further_names(VLLM)) == set(Further)
    assert further_names(MOCK) == {}
    # The computed input is one source of a counter that has several.
    computed = further_names(VLLM)[Further.COMPUTED_INPUT_TOKENS]
    assert (computed.kind, computed.labels) == (Kind.COUNTER, {"source": "local_compute"})
    # The rest are whole families: no label selects a part.
    assert [name for name in every_name(VLLM) if name.labels] == [computed]


def test_every_vllm_name_is_one_section_7_2_gives() -> None:
    section = DESIGN.read_text(encoding="utf-8").split("\n### 7.2 ")[1].split("\n### ")[0]
    given = set(re.findall(r"`(vllm:[a-z_]+)`", section))

    assert all(name.startswith("vllm:") for name in every_name(VLLM))
    assert set(quantity_names(VLLM)) <= given
    # The information metric is vLLM's too: section 7.2 says a label of it carries the capacity,
    # without its name, which the issue gives.
    place = cache_capacity(VLLM)
    assert place is not None
    assert place.metric == "vllm:cache_config_info"


def test_the_mock_has_a_row_and_an_unknown_engine_has_none() -> None:
    assert MOCK in ENGINES
    with pytest.raises(KeyError):
        metric_names("no-such-engine")
    with pytest.raises(KeyError):
        further_names("no-such-engine")
    with pytest.raises(KeyError):
        cache_capacity("no-such-engine")
