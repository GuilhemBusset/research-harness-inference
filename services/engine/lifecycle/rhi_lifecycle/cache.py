"""The engine's cache capacity in tokens, exported as a number (system design, sections 4.7 and 7.2).

How many sessions an instance holds depends on the capacity of its engine's cache: the tokens of
transcript one GPU keeps. An engine fixes it at start, and gives it in its `/metrics` only as a
label of an information metric, which a dashboard cannot plot. The engine's row in
`rhi_common.engine_metrics` says which metric and which label. The agent reads it there and
exports it on its own metrics endpoint (`rhi_lifecycle.metrics`), where the node's collector
scrapes it, as `rhi_lifecycle_engine_cache_capacity_tokens`: a gauge with no label, whose value is
the number of tokens.

vLLM's row names the label `kv_cache_size_tokens` of `vllm:cache_config_info`. Its value is the
number vLLM logs at start as its KV cache size in tokens, so what the agent exports is what the
engine logged. vLLM gives one series for each engine index, each with the capacity of one engine,
and writes `None` in the label where it set no capacity: that reading gives none.

**No reading, no series.** The series exists exactly while the latest reading gave a capacity. It
is absent before the first reading and after a reading that gave none: the engine did not answer,
its answer was not 200 or not Prometheus' text format, its row names no place, the information
metric or its label is missing, the label's value is not a whole number above zero, or two samples
give two capacities. A zero would read as an engine with no cache.

The reading is one of the serving duties: `rhi_lifecycle.serving` hands `Capacity.read` every
answer of the engine's `/metrics` that its heartbeat fetches.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

from prometheus_client import CollectorRegistry
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.parser import text_string_to_metric_families
from prometheus_client.registry import Collector

from rhi_common.engine_metrics import cache_capacity
from rhi_lifecycle import metrics

METRIC = "rhi_lifecycle_engine_cache_capacity_tokens"


def _tokens(written: str) -> int | None:
    """A label's value as a number of tokens: a whole number above zero, or nothing."""
    try:
        number = float(written)
    except ValueError:
        return None
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        return None
    return int(number)


def read_capacity(exposition: bytes, engine: str) -> int | None:
    """The cache capacity in tokens in an answer of the engine's `/metrics`, where `engine`'s row
    says the engine gives it. None when the row names no place or the answer does not give one
    capacity. An engine that labels its series may give several samples: they must agree."""
    place = cache_capacity(engine)
    if place is None:
        return None
    found: set[int | None] = set()
    try:
        for family in text_string_to_metric_families(exposition.decode("utf-8")):
            for sample in family.samples:
                if sample.name == place.metric:
                    found.add(_tokens(sample.labels.get(place.label, "")))
    except ValueError:  # a text that is not UTF-8, or not in Prometheus' format
        return None
    if len(found) != 1:
        return None
    return found.pop()


class Capacity(Collector):
    """The capacity of `engine`'s cache as the agent's metric, in `registry`. It holds what the
    latest reading gave, and the registry has the series only while that is a number."""

    def __init__(self, engine: str, registry: CollectorRegistry = metrics.REGISTRY) -> None:
        cache_capacity(engine)  # an engine with no row is refused here, not at every reading
        self._engine = engine
        self._tokens: int | None = None
        registry.register(self)

    def read(self, answer: tuple[int, bytes] | None) -> int | None:
        """Take one answer of the engine's `/metrics`, a status and a body, or None when the engine
        gave no answer. Return the capacity it gives, which the metric now says, or None."""
        self._tokens = read_capacity(answer[1], self._engine) if answer is not None and answer[0] == 200 else None
        return self._tokens

    def describe(self) -> Iterator[GaugeMetricFamily]:
        # The name the registry keeps for this collector, whether or not it has a series now.
        yield GaugeMetricFamily(METRIC, "The capacity of the engine's cache, in tokens.")

    def collect(self) -> Iterator[GaugeMetricFamily]:
        tokens = self._tokens
        if tokens is not None:
            yield GaugeMetricFamily(METRIC, "The capacity of the engine's cache, in tokens.", value=tokens)
