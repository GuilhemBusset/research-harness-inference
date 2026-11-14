+++
id = "E3-22"
title = "vLLM: the serving settings of a pool's release, as the coursework pool needs them"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E3-04"]
+++

## Context

The engine settings that belong to the coursework pool's release are the ones vLLM's recipe used on
its GPU (section 4.2; evidence E28): the full 262,144-token context; an 8-bit cache; prefix caching;
at most 8 requests running at once, which is also the pool's `target_concurrency` (section 17); a
budget of 8,192 tokens for each engine step, which trades steadier streams against a faster first
token (section 4.7); the `qwen3_coder` tool parser and the `qwen3` reasoning parser. To these the
design adds a served name without a slash, which Claude Code requires (evidence E28), and finer
histogram buckets and cache metrics than vLLM's defaults (section 7.2).

The settings are a pool's configuration, carried into its release's engine flags (section 4.3) by
`src/rhi/pools/vllm_command.py`, whose output `src/rhi/pools/release.py` writes into the release
(issue E3-04), so that the upgrade pool of phase 4, with the same parsers, and any later model are a
configuration entry. The values are set for the coursework pool by issue E3-10.

**The fields and their defaults.** Every field added here is optional, so that a pool that leaves it
out keeps what vLLM does by itself and a merge needs nothing new in the deployed configuration: no
tool-call parser and no reasoning parser unless the pool names one; a cache precision of 16 bits,
vLLM's own for this model; prefix caching on, vLLM's own default; vLLM's own step budget; vLLM's own
bucket boundaries; and no draft head. The design gives no default for these; each default leaves the
command as vLLM would run without the flag. The example configuration holds no vllm pool until issue
E3-10, so it documents the fields in a comment.

What each setting is for:

- the context is the pool's context length, which the gateway also clamps to (section 17);
- the 8-bit cache halves what a token of transcript costs; the 16-bit cache is a lever spike S5
  measures (section 4.7);
- prefix caching is what lets a cached turn start in a second (sections 4.7 and 5.5);
- the served name is the pool's name: students choose a pool by it and the gateway forwards it
  (section 4.2; issue E2-16), and the configuration allows only lower-case letters, digits and
  hyphens in it (issue E2-04), so it never holds a slash;
- vLLM 0.33 can replace the boundaries of three bucket families, `request_latency`,
  `time_to_first_token` and `inter_token_latency` (evidence E3). At 20 to 45 tokens per second a
  token takes 22 to 50 ms, which the default inter-token boundaries (0.01, 0.025, 0.05, 0.075 s)
  split into two buckets (section 7.2);
- the cache metrics give `vllm:kv_block_idle_before_evict_seconds`, how long a block sat idle before
  it was evicted (section 7.2).

vLLM 0.33 also has a flag that maps reasoning effort. The gateway's request profile maps it instead,
so that a unit test proves it without a GPU (section 6.2, step 4), and the engine must not map it a
second time.

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` gives a vllm pool its optional serving settings, with the defaults the Context gives: the tool-call parser, the reasoning parser, the cache precision (8 or 16 bits), whether prefix caching is on, the step budget in tokens, the bucket boundaries of each of vLLM's three bucket families, and the draft head with the number of tokens it proposes.
- [ ] `config.example.toml` documents these fields and their defaults in a comment.
- [ ] `src/rhi/pools/vllm_command.py` passes them: the pool's context length as the served context, the pool's name as the served model name, the cache precision, prefix caching, the pool's target_concurrency as the most requests running at once, the step budget, the tool-call parser with automatic tool choice, the reasoning parser, the bucket boundaries, and the engine's cache metrics turned on; a setting left at its default adds no flag.
- [ ] The command passes the speculative-decoding option of vLLM's recipe for the model, with the pool's number of tokens, only for a pool that turns the draft head on (section 4.7).
- [ ] The command never passes vLLM's own reasoning-effort mapping.
- [ ] `tests/unit/pools/test_vllm_command.py` and `tests/unit/common/test_pool_config.py` cover the settings.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/pools/test_vllm_command.py` passes: for a pool built in the test with the values of section 4.2 (262,144 tokens of context, an 8-bit cache, prefix caching, target_concurrency 8, a step budget of 8,192, parsers qwen3_coder and qwen3), the command carries each of them, the pool's name as the served name, the cache metrics, and the bucket boundaries the pool gives, which for inter-token buckets that split 22 to 50 milliseconds finely are more than two boundaries between 0.022 and 0.05 seconds.
- [ ] `tests/unit/pools/test_vllm_command.py::test_the_draft_head_is_off_by_default` finds no speculative-decoding option for a pool that leaves the draft head out, and finds it with the pool's number of tokens for a pool that turns it on.
- [ ] `tests/unit/pools/test_vllm_command.py::test_no_effort_mapping_in_the_engine` finds no reasoning-effort mapping in the command of any pool.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py` passes: a vllm pool's serving settings parse as written, a pool that leaves them out reads the defaults, and a cache precision other than 8 or 16 bits is refused, naming the pool and the field.

## References

- `docs/design/system-design.md`, sections 4.2, 4.3, 4.7, 6.2, 7.2, 17
- `docs/design/evidence.md`, E3, E28
