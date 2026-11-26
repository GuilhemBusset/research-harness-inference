+++
id = "E3-55"
title = "vLLM pools report the cached part of each request's input in its answer, with --enable-prompt-tokens-details"
labels = ["size:S", "kind:infra"]
epic = 5
+++

## Context

Spike S3's first run on the coursework pool (the 4-bit build) replayed the recorded Claude Code
session through the gateway: 12 requests answered, 249,921 input tokens, and 0 cached input
tokens in the answers. Over the same replay the engine's own counters grew by 250,027 input tokens
and 216,000 cached ones on the instance that served it, 86% cached. So prefix caching works, the
replay sends prefixes a cache can reuse, and the gateway's cache salt is the same for every request
of one student (`services/gateway/rhi_gateway/body.py`, `cache_salt` and `_salt`): what is missing
is the cached part in the answers.

vLLM reports it only when started with `--enable-prompt-tokens-details` (`FrontendArgs`, in
`vllm/entrypoints/common/cli_args.py` at the tag `v0.33.0`, a boolean that is false when left
out). Without it, `_build_anthropic_usage` in `vllm/entrypoints/anthropic/serving.py` leaves
`cache_read_input_tokens` unset and reports the whole prompt as `input_tokens`; the same option
governs the `prompt_tokens_details` of the OpenAI routes. With it, a Messages answer reports `input_tokens` without the cached part and the cached part as
`cache_read_input_tokens`, so the three fields add up to the whole input, as Anthropic defines them
and as the gateway's usage parsers read them (`services/gateway/rhi_gateway/usage/`). This also
settles question 5 of issue #236: vLLM 0.33 does not count cached input twice.

`rhi.pools.vllm_command.arguments` never passes the option, so every answer of a vLLM pool reports
no cached input. The gateway's record of every request (`cached_input_tokens`), the per-student
counter `rhi_gateway_student_cached_input_tokens_total` (section 7.2), the load generator's replay
(`spikes/loadgen.py`, `tokens_of`) and every spike that reads a cached share from the answers read
0 on a vLLM pool. The mock engine reports the field, which is why no test caught it.

The option changes what the engine reports, not what it serves, so the launch fingerprint leaves
it out as it leaves out `--kv-cache-metrics` (`rhi_common.launch_settings`, "What is left out"),
following that module's rule on `CONTRACT`.

Suggested branch: `fix/vllm-prompt-tokens-details`

## Deliverables

- [ ] `src/rhi/pools/vllm_command.py` gives every vllm pool `--enable-prompt-tokens-details`, among the arguments every vllm pool is given, and its docstring says where vLLM 0.33.0 defines the option and what an answer reports without it.
- [ ] `services/common/rhi_common/launch_settings.py` leaves `--enable-prompt-tokens-details` out of the fingerprint, as an option whose value is never read, beside `--kv-cache-metrics`, and its docstring names it.
- [ ] `tests/unit/pools/test_vllm_command.py` asserts that every vllm pool's arguments hold `--enable-prompt-tokens-details` exactly once, whatever its loader and serving settings.
- [ ] `tests/unit/common/test_launch_settings.py` asserts that adding `--enable-prompt-tokens-details` to a release's arguments leaves its fingerprint unchanged and never makes it `UNAVAILABLE`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/pools/test_vllm_command.py tests/unit/common/test_launch_settings.py tests/synth/test_pool_release.py` passes.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] A rerun of spike S3 on the coursework pool writes a `replay` line whose `cached_input_tokens` is above zero and within five percent of the growth of the `cached_input_tokens` counters its two `engine_counters` lines show, summed over the instances (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.1, 4.2 and 7.2
- `src/rhi/pools/vllm_command.py`
- `services/common/rhi_common/launch_settings.py`
- `services/gateway/rhi_gateway/usage/messages.py`
- `spikes/s3_harness_task.py`, the replay and its counters
