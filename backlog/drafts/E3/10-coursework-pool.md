+++
id = "E3-10"
title = "The coursework pool: Qwen3.8-27B at 8 bits on one 96 GB GPU, with two warm instances"
labels = ["size:M", "kind:infra"]
epic = "E3"
blocked_by = ["E2-68", "E2-69", "E3-01", "E3-03", "E3-09", "E3-22", "E3-38"]
+++

## Context

The coursework pool is the first GPU pool built (section 4.2). There is no cheaper GPU tier below it:
a smaller model would run a different chat template and different parsers, and would exercise none
of the request handling this model needs. Its entry, from the design's own example and the defaults
of section 17:

- the model Qwen/Qwen3.8-27B-FP8, the publisher's 8-bit build, 30.9 GB (evidence E28), at the
  revision its manifest pins (issue E3-09);
- vLLM 0.33 or later on `g7e.2xlarge`: one 96 GB RTX PRO 6000 on 4 cores, at $3.36 an hour
  (evidence E5). The design lists no fallback type for this pool;
- `max_instances` 2, `warm_instances` 2, `wake` on-demand, `target_concurrency` 8 (the engine's
  limit on requests running at once), `hold_seconds` 420, `idle_minutes` 20, `drain_seconds` 300,
  `gpu_hours_per_day` 16. The monthly ceiling, 150 GPU-hours, arrives with issue E4-02;
- the full 262,144-token context, which keeps Claude Code clear of a context error it cannot recover
  from (sections 4.2 and 6.1);
- the serving settings of section 4.2, which issue E3-22 makes fields of a vllm pool: an 8-bit cache,
  prefix caching, a step budget of 8,192 tokens, the `qwen3_coder` tool parser, the `qwen3` reasoning
  parser, and histogram buckets finer than vLLM's defaults. The served name is the pool's name,
  coursework, as for every vllm pool (issue E3-22). The loader and the exception for model code keep
  their defaults: the streamer, and no exception, since the model needs no code from its repository
  (evidence E28);
- the expected values of section 4.7, which the inference dashboard draws beside what the pool
  measures (sections 4.7 and 7.3), in the shape of issue E2-68: 15 to 22 tokens per second per
  stream at eight agent streams, 0.3 to 1.5 seconds to the first token of a turn whose transcript is
  cached, 1.1 to 1.8 million tokens of cache. Spike S5's record replaces them (issue E3-42).

The coursework pool is the default pool (section 4.2): the gateway sends `claude-*` model names to it
(section 6.2, step 4), so the example's default pool becomes coursework.

The pool's largest output is not a number the design gives. Claude Code asks for up to 32,000 output
tokens on its main turns (evidence E15), and the body filter clamps to the pool's largest output, so
32,000 is the value that leaves Claude Code's requests as they are sent.

**What the owner does, and when.** The example configuration holds this entry for tests and
synthesis. What runs in the account is the owner's configuration in SSM, so merging this issue starts
nothing, and a deployment without the pool in SSM builds no coursework pool. The launch template
reads the image from its parameter when the stack is deployed (issue E3-01), so the parameter must
hold a value before the pool is in the deployed configuration. The promotion job of issue E3-02
cannot give the first one: it starts its canary from the launch template of the pool's group, which
exists only once the pool does. The owner therefore writes the first value by hand, then adds the
pool to SSM; every later image goes through the job. The machine image is AWS's base GPU image with
the open-source NVIDIA driver on Ubuntu 22.04, whose release of 10 November 2026 lists G7e (section
4.3; evidence E10). The pool's group, its volume and their synthesis are issue E3-40; its request
profile is issue E3-27; its GPU quota is an obligation of issue E1-13.

The pool cannot serve from this issue alone: its instances need the 150 GB volume of issue E3-40,
and the lifecycle agent counts the engine's requests by the vLLM row of issue E3-06. A pool added to
SSM before both would fail its launches until the scaler suspends it, and merging E3-40 later would
refresh nothing, since a volume is not part of a release. So the two owner steps, the image
parameter and then the pool in SSM, are items of issue E3-40, which comes after both.

## Deliverables

- [ ] `config.example.toml` adds g7e.2xlarge to the allowed instance types and gains a pool named coursework: engine vllm, model Qwen/Qwen3.8-27B-FP8 at the revision of its manifest, instance types g7e.2xlarge alone, priced at 3.36 dollars an hour, max_instances 2, warm_instances 2, wake on-demand, target_concurrency 8, hold_seconds 420, idle_minutes 20, drain_seconds 300, gpu_hours_per_day 16, a context length of 262,144 tokens and a largest output of 32,000 tokens.
- [ ] The coursework entry gives the expected values of section 4.7: 15 to 22 tokens per second per stream, 0.3 to 1.5 seconds to the first token of a cached turn, and 1,100,000 to 1,800,000 tokens of cache; it gives no measured start until spike S1 has one.
- [ ] The coursework entry's serving settings are an 8-bit cache, prefix caching, a step budget of 8,192 tokens, the tool-call parser qwen3_coder, the reasoning parser qwen3, and inter-token latency buckets that split 22 to 50 milliseconds into more than the two buckets vLLM's defaults give.
- [ ] The example configuration's default pool becomes coursework, so that claude- model names reach it.
- [ ] `tests/unit/test_config.py` gains test_the_example_coursework_pool, which loads `config.example.toml` and reads the coursework pool.

## Acceptance criteria

- [ ] `tests/unit/test_config.py::test_the_example_coursework_pool` passes: the coursework pool's daily ceiling reads 16 GPU-hours, its expected values those of section 4.7, its context 262,144 tokens and its largest output 32,000, its revision is the one of `models/manifests/qwen--qwen3.8-27b-fp8.json`, and the default pool is coursework.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py tests/unit/test_config.py` passes with the pools of `config.example.toml`.
- [ ] `scripts/check.sh synth` exits 0 with the example configuration's coursework pool.
- [ ] The owner accepts Qwen3.8-27B on one 96 GB GPU as the coursework model, and that it answers "a cheap model first", decision 2 of the design (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 4.2, 4.3, 4.7, 5.2, 6.1, 11.1, 15, 17
- `docs/design/evidence.md`, E5, E10, E15, E28
