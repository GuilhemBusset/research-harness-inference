+++
id = "E7-08"
title = "Read session capacity and its limiting rows from evidence"
labels = ["size:M", "kind:test"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

This is an offline reader over typed input. The controller later supplies the scheduler and workspace observations. Read deployed thresholds from the manifest.

## Deliverables

- [ ] `tools/evaluate/capacity.py` evaluates each class-size shape and repetition using the deployed S5 thresholds, a minimum of 100 eligible later turns and streams, complete attempt accounting and evidence for preemptions on every host.
- [ ] It reports per-stream p50/p10, later and first-turn p95 separately, failed attempts and Wilson interval, cached share, per-host and aggregate engine throughput, delivered tokens, queueing, GPU utilization and host cost per million generated tokens.
- [ ] It distinguishes measured misses, insufficient evidence and invalid cells while retaining a known miss alongside an incomplete result.
- [ ] `tools/evaluate/tests/test_capacity.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_capacity.py` tests threshold equality for every floor, midstream and connection failures, a missing host series, driver lag, skipped openings and exactly 99 versus 100 eligible samples.
- [ ] `tools/evaluate/tests/test_capacity.py` recomputes a multihost example with different service times through drain, reports opening and warm-up outside steady state and exposes a failing repeat even when pooling repeats would pass.

## References

- `docs/design/agent-session-evaluation.md`, Reading a result
- `docs/measurements/s5.md`
