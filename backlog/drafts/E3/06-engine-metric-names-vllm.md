+++
id = "E3-06"
title = "The vLLM row of the per-engine metric table, with the names of section 7.2 and its cache capacity"
labels = ["size:M", "kind:app"]
epic = "E3"
blocked_by = ["E2-66", "E2-82"]
+++

## Context

Engines name their metrics differently; dashboards, spikes and the lifecycle agent read them through
a small per-engine table, so adding an engine is one adapter and one table row (section 4.1). The
engine contract's metrics row names at least: running and waiting requests; time to first token;
time per output token; input tokens, the cached part of them, and output tokens; cache use. The
mock's row names those quantities (issue E2-82), and the table has a place for where an engine gives
its cache capacity in tokens, which the lifecycle agent reads and exports as a number (issue E2-66).

Section 7.2 gives vLLM's names at version 0.33, and evidence E3 confirms each at that tag:

| Quantity | vLLM 0.33 |
| --- | --- |
| Requests running, requests waiting | `vllm:num_requests_running`, `vllm:num_requests_waiting` |
| Time to first token | `vllm:time_to_first_token_seconds` |
| Time per output token | `vllm:request_time_per_output_token_seconds` |
| Input tokens, cached ones included | `vllm:prompt_tokens` |
| The cached part of the input | `vllm:prompt_tokens_cached` |
| Output tokens | `vllm:generation_tokens` |
| Cache use | `vllm:kv_cache_usage_perc` |

Section 7.2 reads more from vLLM than the contract asks of every engine, for the quantities of
section 4.7 that only an engine with a real cache and real prefill has: the input actually computed,
`vllm:prompt_tokens_by_source` with the source `local_compute`; the two parts of the time to first
token, `vllm:request_queue_time_seconds` and `vllm:request_prefill_time_seconds`; the decode time,
`vllm:request_decode_time_seconds`; input and output tokens per request, `vllm:request_prompt_tokens`
and `vllm:request_generation_tokens`; tokens computed in prefill per request,
`vllm:request_prefill_kv_computed_tokens`; preemptions, `vllm:num_preemptions`; and how long a block
sat idle before it was evicted, `vllm:kv_block_idle_before_evict_seconds`, with the engine's cache
metrics on (issue E3-22). The inference dashboard and spike S5 read them, so the table carries them
too, as quantities an engine may leave out: the mock does.

vLLM logs its cache capacity at start and carries it in its metrics only as labels of
`vllm:cache_config_info` (section 7.2; evidence E3), which is the vLLM row's capacity entry. Which
labels give the capacity in tokens at version 0.33 is read from vLLM's source at that tag, and the
number the agent exports must equal the one the engine logs.

vLLM exposes counters with a `_total` suffix, and its only labels are the model's name and an engine
index (evidence E3), so a query needs each name's type. vLLM hides a metric one minor version after
deprecating it and removes it the next, so the row belongs to the pinned version (issue E3-03).

## Deliverables

- [ ] `services/common/rhi_common/engine_metrics.py` gains the vLLM row: a name of section 7.2 for every quantity of the contract's metrics row, and its capacity entry, `vllm:cache_config_info` with the labels that give the capacity in tokens.
- [ ] The table gains the further quantities section 7.2 reads from vLLM, listed in the Context, which an engine's row may leave out; the vLLM row names each of them, and the mock's row leaves them out.
- [ ] Each name of every row says whether it is a counter, a gauge or a histogram, and a label that selects part of it where it needs one, so that a query adds the counter suffix, reads a histogram's buckets, sum and count, or selects the computed input.
- [ ] `services/engine/lifecycle/rhi_lifecycle/cache.py` reads vLLM's capacity in tokens from the labels the vLLM row names.
- [ ] `tests/unit/common/test_engine_metrics.py` and `tests/unit/lifecycle/test_cache.py` cover the row and the reading of vLLM's capacity, the latter on an exposition of vLLM 0.33's information metric written in the test from vLLM's source.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_engine_metrics.py` passes: every engine's row names every quantity of the contract's metrics row and a capacity entry, no name is empty, every vLLM name starts with vllm: and is one section 7.2 gives, and every name has a type.
- [ ] `uv run --locked pytest tests/unit/lifecycle/test_cache.py` passes: the agent's capacity metric for the vLLM exposition of the test equals the capacity in tokens that exposition describes.
- [ ] On the coursework pool, the engine's metrics route exposes every name of the vLLM row at the pinned version, and the capacity the lifecycle agent reports equals the one the engine logged at start (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.1, 4.7, 7.2
- `docs/design/evidence.md`, E3
