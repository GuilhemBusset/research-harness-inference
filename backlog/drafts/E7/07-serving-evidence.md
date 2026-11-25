+++
id = "E7-07"
title = "Verify host provenance and telemetry over the measured window"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-01", "E7-06"]
+++

## Context

Reuse S5 range readers, the lifecycle release series and gateway counters. The requested release alone cannot establish what a pool served.

## Deliverables

- [ ] `tools/evaluate/telemetry.py` resolves requested and observed serving conditions, including actual launch fingerprint, host type/count, image/model/tokenizer and gateway profile provenance, and reads the design’s per-host counter and gauge metrics through drain.
- [ ] The reader handles scrape delivery, counter resets, incomplete coverage, unexpected releases, membership changes and aggregate canary-versus-pool traffic reconciliation including side queries, while reducing private labels to local ordinals.
- [ ] The reader records preflight, warm-up coverage on every host and cache evidence, and marks unverifiable treatments or contaminated windows with a fixed reason.
- [ ] `tools/evaluate/tests/test_telemetry.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_telemetry.py` injects a reset, a missing preemption series, mixed flags at the same revision, host turnover and missing gateway provenance and checks which results are unverified.
- [ ] `tools/evaluate/tests/test_telemetry.py` separates overlapping gateway smoke traffic from ordinary node liveness generation, excludes setup probes and reports per-host and pool totals using the actual elapsed window.

## References

- `docs/design/agent-session-evaluation.md`, Reproducing and comparing runs
- `spikes/s5_capacity.py`
- `services/common/rhi_common/engine_metrics.py`
