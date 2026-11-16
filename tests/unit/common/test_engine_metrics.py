"""The per-engine table of metric names (`rhi_common.engine_metrics`; system design, section 4.1)."""

from __future__ import annotations

import pytest

from rhi_common.engine_metrics import ENGINES, MOCK, NAME, Kind, Quantity, metric_names


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
    names = list(row.values())
    assert len(set(names)) == len(names)
    assert all(NAME.fullmatch(name) for name in names)
    # A counter's family name never carries the suffix of its sample.
    assert not any(row[quantity].endswith("_total") for quantity in Quantity if quantity.kind is Kind.COUNTER)


def test_the_mock_has_a_row_and_an_unknown_engine_has_none() -> None:
    assert MOCK in ENGINES
    with pytest.raises(KeyError):
        metric_names("no-such-engine")
