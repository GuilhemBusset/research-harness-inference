"""The per-engine table of metric names (system design, sections 4.1 and 7.2).

The engine contract's metrics row names quantities, and every engine exports them under names of
its own. Dashboards and the lifecycle agent read an engine's `/metrics` through this table: one row
per engine, which names every quantity of the row. Adding an engine is one adapter and one row.

A name is the name of a metric family, as Prometheus' text format declares it in its `# TYPE`
line, and it says what a query needs beside it (`Name`). Its `kind`: a counter's sample carries the
family name with `_total` after it, a histogram's carries `_bucket`, `_sum` and `_count`, a gauge's
carries the name alone. Its `labels`, where the quantity is only part of the family: the label
values that select that part.

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

**The further quantities.** Section 7.2 reads more than the contract asks of every engine: the
quantities of section 4.7 that only an engine with a real cache and a real prefill has (`Further`).
A row may leave any of them out, and the mock's leaves out all of them.

| Further quantity | Kind in vLLM's row | What it measures |
| --- | --- | --- |
| `computed_input_tokens` | counter | the part of the input tokens the engine computed: not read from a cache |
| `queue_time` | histogram | seconds a request waited before it ran: the first part of the time to first token |
| `prefill_time` | histogram | seconds a request spent computing its input: the second part |
| `decode_time` | histogram | seconds a request spent generating its output |
| `request_input_tokens` | histogram | input tokens of one request |
| `request_output_tokens` | histogram | output tokens of one request |
| `request_prefill_computed_tokens` | histogram | tokens one request computed in prefill, cached ones left out |
| `preemptions` | counter | requests the engine put back in its queue to free cache |
| `block_idle_before_eviction` | histogram | seconds a block of the cache sat idle before it was evicted |

**The cache capacity.** The capacity of an engine's cache in tokens (section 4.7) is not a quantity
of the rows above. An engine fixes it at start and gives it where it chooses: vLLM, as a label of an
information metric, whose value is always 1 and which a dashboard cannot plot. So an engine's row
also has a place for where the engine gives it, `cache_capacity`: the information metric and the
label whose value is the number of tokens. The lifecycle agent reads it there and exports it as a
number (section 7.2). A row that does not say where gives none, and the agent then exports none.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
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


class Further(StrEnum):
    """One quantity section 7.2 reads beside the contract's, which an engine's row may leave out."""

    COMPUTED_INPUT_TOKENS = "computed_input_tokens"
    QUEUE_TIME = "queue_time"
    PREFILL_TIME = "prefill_time"
    DECODE_TIME = "decode_time"
    REQUEST_INPUT_TOKENS = "request_input_tokens"
    REQUEST_OUTPUT_TOKENS = "request_output_tokens"
    REQUEST_PREFILL_COMPUTED_TOKENS = "request_prefill_computed_tokens"
    PREEMPTIONS = "preemptions"
    BLOCK_IDLE_BEFORE_EVICTION = "block_idle_before_eviction"


# A metric family's name, and a label's, as Prometheus' data model allows them.
NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
LABEL = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


class Name(str):
    """A metric family's name in a row. It is the name itself, a string, and it says what a query
    needs beside it: `kind`, the family's Prometheus type, and `labels`, the label values that
    select the quantity where it is only part of the family, empty where it is the whole of it."""

    kind: Kind
    labels: Mapping[str, str]

    def __new__(cls, name: str, kind: Kind, labels: Mapping[str, str] | None = None) -> Self:
        made = super().__new__(cls, name)
        made.kind = kind
        made.labels = MappingProxyType(dict(labels or {}))
        return made

    def __reduce__(self) -> tuple[type[Self], tuple[str, Kind, dict[str, str]]]:
        # What a copy is built from: a string's own answer would leave the kind and the labels out.
        return type(self), (str(self), self.kind, dict(self.labels))


@dataclass(frozen=True)
class CacheCapacity:
    """Where an engine gives its cache capacity in tokens: the information metric, whose value is
    always 1, and the label of it whose value is the number of tokens."""

    metric: str
    label: str


@dataclass(frozen=True)
class _Row:
    """One engine's row: the name of each quantity's metric, the names of the further quantities
    the engine has, and where the engine gives its cache capacity, when the row says."""

    names: Mapping[Quantity, Name]
    further: Mapping[Further, Name] = MappingProxyType({})
    cache_capacity: CacheCapacity | None = None


MOCK = "mock"
VLLM = "vllm"

_TABLE: dict[str, _Row] = {
    MOCK: _Row(
        names=MappingProxyType(
            {
                Quantity.RUNNING_REQUESTS: Name("rhi_mock:num_requests_running", Kind.GAUGE),
                Quantity.WAITING_REQUESTS: Name("rhi_mock:num_requests_waiting", Kind.GAUGE),
                Quantity.TIME_TO_FIRST_TOKEN: Name("rhi_mock:time_to_first_token_seconds", Kind.HISTOGRAM),
                Quantity.TIME_PER_OUTPUT_TOKEN: Name("rhi_mock:time_per_output_token_seconds", Kind.HISTOGRAM),
                Quantity.INPUT_TOKENS: Name("rhi_mock:prompt_tokens", Kind.COUNTER),
                Quantity.CACHED_INPUT_TOKENS: Name("rhi_mock:prompt_tokens_cached", Kind.COUNTER),
                Quantity.OUTPUT_TOKENS: Name("rhi_mock:generation_tokens", Kind.COUNTER),
                Quantity.CACHE_USAGE: Name("rhi_mock:kv_cache_usage_perc", Kind.GAUGE),
            }
        ),
        cache_capacity=CacheCapacity(metric="rhi_mock:cache_config_info", label="capacity_tokens"),
    ),
    # vLLM's names at version 0.33, the version `services/engine/vllm/Dockerfile` pins: those of
    # the system design, section 7.2 (evidence E3). vLLM hides a metric one minor version after
    # deprecating it and removes it the next, so this row is that version's. Each type, and the
    # label of the computed input, is the one vLLM's source declares at the tag v0.33.0
    # (`vllm/v1/metrics/loggers.py`, class `PrometheusStatLogger`).
    VLLM: _Row(
        names=MappingProxyType(
            {
                Quantity.RUNNING_REQUESTS: Name("vllm:num_requests_running", Kind.GAUGE),
                Quantity.WAITING_REQUESTS: Name("vllm:num_requests_waiting", Kind.GAUGE),
                Quantity.TIME_TO_FIRST_TOKEN: Name("vllm:time_to_first_token_seconds", Kind.HISTOGRAM),
                Quantity.TIME_PER_OUTPUT_TOKEN: Name("vllm:request_time_per_output_token_seconds", Kind.HISTOGRAM),
                Quantity.INPUT_TOKENS: Name("vllm:prompt_tokens", Kind.COUNTER),
                Quantity.CACHED_INPUT_TOKENS: Name("vllm:prompt_tokens_cached", Kind.COUNTER),
                Quantity.OUTPUT_TOKENS: Name("vllm:generation_tokens", Kind.COUNTER),
                Quantity.CACHE_USAGE: Name("vllm:kv_cache_usage_perc", Kind.GAUGE),
            }
        ),
        further=MappingProxyType(
            {
                # One counter for every source of input tokens: `local_compute` is the input the
                # engine computed, beside `local_cache_hit` and `external_kv_transfer`
                # (`vllm/v1/metrics/stats.py`, `PromptTokenStats`).
                Further.COMPUTED_INPUT_TOKENS: Name(
                    "vllm:prompt_tokens_by_source", Kind.COUNTER, {"source": "local_compute"}
                ),
                Further.QUEUE_TIME: Name("vllm:request_queue_time_seconds", Kind.HISTOGRAM),
                Further.PREFILL_TIME: Name("vllm:request_prefill_time_seconds", Kind.HISTOGRAM),
                Further.DECODE_TIME: Name("vllm:request_decode_time_seconds", Kind.HISTOGRAM),
                Further.REQUEST_INPUT_TOKENS: Name("vllm:request_prompt_tokens", Kind.HISTOGRAM),
                Further.REQUEST_OUTPUT_TOKENS: Name("vllm:request_generation_tokens", Kind.HISTOGRAM),
                Further.REQUEST_PREFILL_COMPUTED_TOKENS: Name(
                    "vllm:request_prefill_kv_computed_tokens", Kind.HISTOGRAM
                ),
                Further.PREEMPTIONS: Name("vllm:num_preemptions", Kind.COUNTER),
                # Exported only with the engine's cache metrics on (`kv_cache_metrics`, `vllm/config/observability.py`).
                Further.BLOCK_IDLE_BEFORE_EVICTION: Name("vllm:kv_block_idle_before_evict_seconds", Kind.HISTOGRAM),
            }
        ),
        # The labels of `vllm:cache_config_info` are the attributes of the engine's cache
        # configuration, each written as text (`vllm/config/cache.py`, `CacheConfig.metrics_info`).
        # `kv_cache_size_tokens` is the capacity in tokens: the number the engine logs at start as
        # "KV cache size: ... tokens" (`vllm/v1/core/kv_cache_utils.py`,
        # `update_kv_cache_capacity`, which stores it and logs it), handed to the process that
        # serves `/metrics` when the engine is ready (`vllm/v1/engine/core_client.py`,
        # `_apply_ready_response`). It is one capacity for each engine index, never a sum, and it
        # reads `None` where the engine set none. The number of blocks times the block size is not
        # it: vLLM's source says that product can be wrong for a model whose requests take blocks
        # in several groups, and the number of blocks is summed over the engine indexes.
        cache_capacity=CacheCapacity(metric="vllm:cache_config_info", label="kv_cache_size_tokens"),
    ),
}

ENGINES = tuple(_TABLE)


def _row(engine: str) -> _Row:
    if engine not in _TABLE:
        message = f"no metric names for the engine {engine!r}: expected one of {sorted(_TABLE)}"
        raise KeyError(message)
    return _TABLE[engine]


def metric_names(engine: str) -> Mapping[Quantity, Name]:
    """The engine's row: the name of each quantity's metric. Raises `KeyError` for an engine with no row."""
    return _row(engine).names


def further_names(engine: str) -> Mapping[Further, Name]:
    """The names of the further quantities the engine's row has: none of them, some or all.
    Raises `KeyError` for an engine with no row."""
    return _row(engine).further


def cache_capacity(engine: str) -> CacheCapacity | None:
    """Where the engine gives its cache capacity in tokens, or None when its row does not say.
    Raises `KeyError` for an engine with no row."""
    return _row(engine).cache_capacity
