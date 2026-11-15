"""The per-engine table of metric names (system design, sections 4.1 and 7.2).

The engine contract's metrics row names quantities, and every engine exports them under names of
its own. Dashboards and the lifecycle agent read an engine's `/metrics` through this table: one row
per engine, which names every quantity of the row. Adding an engine is one adapter and one row.

A name is the name of a metric family, as Prometheus' text format declares it in its `# TYPE`
line. A counter's sample carries the family name with `_total` after it; a histogram's carries
`_bucket`, `_sum` and `_count`.

| Quantity | Kind | What it measures |
| --- | --- | --- |
| `running_requests` | gauge | requests being generated now |
| `waiting_requests` | gauge | requests accepted and waiting to run |
| `time_to_first_token` | histogram | seconds from a request's arrival to its first output token |
| `time_per_output_token` | histogram | seconds between two output tokens of one request |
| `input_tokens` | counter | input tokens of the requests run, cached ones included |
| `cached_input_tokens` | counter | the part of the input tokens read from the cache |
| `output_tokens` | counter | output tokens generated |
| `cache_usage` | gauge | the share of the cache in use, from 0 to 1 |
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Self


class Kind(StrEnum):
    """The Prometheus type of a metric."""

    GAUGE = "gauge"
    COUNTER = "counter"
    HISTOGRAM = "histogram"


class Quantity(StrEnum):
    """One quantity of the contract's metrics row, with the kind every engine exports it as."""

    kind: Kind

    def __new__(cls, name: str, kind: Kind) -> Self:
        quantity = str.__new__(cls, name)
        quantity._value_ = name
        quantity.kind = kind
        return quantity

    RUNNING_REQUESTS = ("running_requests", Kind.GAUGE)
    WAITING_REQUESTS = ("waiting_requests", Kind.GAUGE)
    TIME_TO_FIRST_TOKEN = ("time_to_first_token", Kind.HISTOGRAM)
    TIME_PER_OUTPUT_TOKEN = ("time_per_output_token", Kind.HISTOGRAM)
    INPUT_TOKENS = ("input_tokens", Kind.COUNTER)
    CACHED_INPUT_TOKENS = ("cached_input_tokens", Kind.COUNTER)
    OUTPUT_TOKENS = ("output_tokens", Kind.COUNTER)
    CACHE_USAGE = ("cache_usage", Kind.GAUGE)


# A metric family's name, as Prometheus' data model allows it.
NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")

MOCK = "mock"

_TABLE: dict[str, Mapping[Quantity, str]] = {
    MOCK: MappingProxyType(
        {
            Quantity.RUNNING_REQUESTS: "rhi_mock:num_requests_running",
            Quantity.WAITING_REQUESTS: "rhi_mock:num_requests_waiting",
            Quantity.TIME_TO_FIRST_TOKEN: "rhi_mock:time_to_first_token_seconds",
            Quantity.TIME_PER_OUTPUT_TOKEN: "rhi_mock:time_per_output_token_seconds",
            Quantity.INPUT_TOKENS: "rhi_mock:prompt_tokens",
            Quantity.CACHED_INPUT_TOKENS: "rhi_mock:prompt_tokens_cached",
            Quantity.OUTPUT_TOKENS: "rhi_mock:generation_tokens",
            Quantity.CACHE_USAGE: "rhi_mock:kv_cache_usage_perc",
        }
    ),
}

ENGINES = tuple(_TABLE)


def metric_names(engine: str) -> Mapping[Quantity, str]:
    """The engine's row: the name of each quantity's metric. Raises `KeyError` for an engine with no row."""
    if engine not in _TABLE:
        message = f"no metric names for the engine {engine!r}: expected one of {sorted(_TABLE)}"
        raise KeyError(message)
    return _TABLE[engine]
