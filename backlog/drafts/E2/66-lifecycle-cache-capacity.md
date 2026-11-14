+++
id = "E2-66"
title = "Lifecycle agent: report the engine's cache capacity in tokens, as a number"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-35", "E2-75"]
+++

## Context

How many sessions an instance holds depends on its cache capacity: the tokens of transcript one GPU
keeps for running and recent requests (section 4.7). The inference dashboard shows the capacity each
instance reports beside the pool's expected capacity from configuration (section 7.3; issue E2-68),
and spike S5 asks what capacity the engine reports at start. vLLM logs that capacity at start and
carries it in its metrics only as a label, which a dashboard cannot plot, so the lifecycle agent
exports it as a number (section 7.2).

Where an engine gives its capacity belongs in the per-engine table, beside the names of its other
metrics (section 4.1). This issue adds the mock's entry, and the mock gives the capacity of its
imitated prefix cache (issue E2-75) the way vLLM gives its own, as a label of an information metric,
so that the whole path runs in phase 1. The vLLM row's entry belongs with the rest of that row
(issue E3-06).

## Deliverables

- [ ] `services/common/rhi_common/engine_metrics.py` gives an engine's row a place for where the engine gives its cache capacity in tokens, and the mock's row fills it with an information metric of the mock and the label that carries it; no other row is filled here.
- [ ] `services/engine/mock/rhi_mock_engine/app.py` serves that information metric on its metrics route, with the capacity of the mock's imitated cache as the label's value.
- [ ] `services/engine/lifecycle/rhi_lifecycle/cache.py` reads, once the engine serves, its cache capacity in tokens as the engine's row says, and exposes it on the agent's metrics endpoint of issue E2-35, for the node's collector, as a metric whose value is that number, and `services/engine/lifecycle/rhi_lifecycle/serving.py` runs that reading among its serving duties.
- [ ] While the capacity cannot be read, the agent exposes no capacity metric, never a zero.
- [ ] `tests/unit/lifecycle/test_cache.py` covers the reading against the mock engine.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle/test_cache.py tests/unit/common/test_engine_metrics.py` passes: with the mock set to a capacity of 1,500,000 tokens, the agent's capacity metric reads 1500000.
- [ ] `tests/unit/lifecycle/test_cache.py::test_no_capacity_no_series` shows no capacity metric while the engine gives none.

## References

- `docs/design/system-design.md`, sections 4.1, 4.7, 7.2, 7.3
