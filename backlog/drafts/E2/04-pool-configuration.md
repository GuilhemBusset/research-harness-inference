+++
id = "E2-04"
title = "Parse and validate a pool's entry: the fields of section 4.2, its limits and its defaults"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-02"]
+++

## Context

A pool is one model on one family of instance types, declared in configuration; it becomes one Auto
Scaling group, one target group and one dashboard row (section 4.2). The constructs, the scaler,
the gateway, the lifecycle agent and the dashboards read the same entry, so its parsing and its
rules live in the shared package, with no dependency on the app's configuration loader: issue E2-69
makes the loader use it. Section 4.2 gives the entry's fields, with the coursework pool as its
example:

```toml
[pools.coursework]
engine = "vllm"
model = "Qwen/Qwen3.8-27B-FP8"
revision = "<pinned commit of the model repository>"
instance_types = ["g7e.2xlarge"]
max_instances = 2
warm_instances = 2
wake = "on-demand"
target_concurrency = 8
hold_seconds = 420
idle_minutes = 20
drain_seconds = 300
gpu_hours_per_day = 16
```

Other sections need more of a pool, and this issue adds three fields:

- `context_length` and `largest_output`, in tokens: the limits the gateway's body filter clamps to
  are the pool's own (section 17).
- `measured_start_seconds`, optional: the launch hook's deadline becomes twice the measured start
  once spike S1 has measured one (sections 4.3 and 17).
- `hold_seconds_non_streaming`, 240 by default: section 5.3 holds a non-streaming call for 240
  seconds, against 420 for a stream, and section 17 gives both as defaults. The gateway's hold
  (issue E2-25) reads this field, not a constant.

Section 17 gives defaults for the hold (420 seconds for a stream, 240 for a non-streaming call), the
idle time (20 minutes) and the drain (300 seconds), so those may be left out. The example's
`target_concurrency` of 8 and 16 GPU-hours a day are the coursework pool's values (section 17), not
defaults for every pool: every pool gives its target_concurrency, and every vllm pool its
gpu_hours_per_day. `warm_instances` is the number of prepared instances, running or stopped, that
the group keeps, with a warm pool minimum of zero (section 4.2): from 0, which the upgrade pool uses,
to `max_instances`. The engines are `mock` and `vllm`; `sglang` is not planned (section 4.1).
Students choose a pool by model name, and Claude Code requires a served name without a slash
(section 4.2), so the pool's name is that model name.

The prices of a pool's instance types, the on-demand price limit and the expected values are issue
E2-68; the top-level keys (the allowed instance types, the default pool) and the example's mock pool
are issue E2-69.

**Every pool field added after this issue has a default or is optional.** The owner writes the
configuration to SSM once, and the pipeline loads it on every deployment with a loader that fails
closed (issue E1-03): a field that every pool must give, added once the owner has written a pool,
would fail the next deployment until the owner wrote it. The fields this issue requires are safe
because the deployed pools table is still empty: the first pool reaches SSM with issue E2-71. The
pre-warm lead, the monthly GPU-hour ceiling and the affinity settings come later, each with its
default (issues E4-05, E4-02 and E3-28).

## Deliverables

- [ ] `services/common/rhi_common/pool_config.py` parses the pools of the configuration into typed pools with the fields of section 4.2, and gives a field left out only the default of section 17: hold_seconds 420, hold_seconds_non_streaming 240, idle_minutes 20 and drain_seconds 300.
- [ ] Each pool also gives context_length and largest_output, in tokens, and may give measured_start_seconds; a pool without measured_start_seconds has none, not a default.
- [ ] A pool's name, the model name students choose it by, holds only lower-case letters, digits and hyphens.
- [ ] It refuses, naming the pool and the field: an unknown field; an engine other than mock and vllm; a wake other than on-demand and scheduled-only; max_instances or target_concurrency below 1; warm_instances below 0 or above max_instances; a context_length, largest_output or hold below 1; and, for a vllm pool, a missing model, a revision that is not a 40-character commit hash, or a missing gpu_hours_per_day.
- [ ] `tests/unit/common/test_pool_config.py` covers the defaults, the three added fields and each refusal.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py` passes: the coursework entry of section 4.2, with a 40-character revision, a context length and a largest output added, parses to the values of section 4.2, and the same entry without hold_seconds, idle_minutes and drain_seconds parses to 420, 20 and 300, with a non-streaming hold of 240.
- [ ] `tests/unit/common/test_pool_config.py::test_refusals` shows a refusal naming the pool and the field for the engine sglang, a pool name with a slash, warm_instances 3 with max_instances 2, an unknown field, and a vllm pool whose revision is main.
- [ ] `tests/unit/common/test_pool_config.py::test_optional_fields` shows a mock pool with no model, no revision, no gpu_hours_per_day and no measured start accepted, and a hold_seconds_non_streaming of 120 read as given.

## References

- `docs/design/system-design.md`, sections 4.1, 4.2, 4.3, 5.3, 17
