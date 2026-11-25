+++
id = "E7-23"
title = "Execute warm-up, capacity, opening and live phases as planned"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-07", "E7-17", "E7-22"]
+++

## Context

This phase runner consumes an admitted booking and the plan. The operator controller handles external session ownership and publication in a separate issue.

## Deliverables

- [ ] `tools/evaluate/phases.py` binds the scheduler, telemetry reader, tasks and journal to each planned measurement phase, including route and per-host warm-up and class opening.
- [ ] The runner resolves the installed versions and digests of actual harness, task, grader and workload artifacts into the manifest before measuring.
- [ ] It takes a cell baseline, waits for scrape delivery, validates distinct canary identity/cache conditions and accounting over the whole window, then emits attempt and host observations for offline grading.
- [ ] It checks cancellation, deadline and release/booking continuity during every cell, stops all traffic with bounded drain, and leaves partial evidence recoverable.
- [ ] `tools/evaluate/tests/test_phases.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_phases.py` executes a reduced plan against local transports and fake telemetry, obtains capacity and live-task records for both shapes and keeps warm-up and class openings outside steady-state samples.
- [ ] `tools/evaluate/tests/test_phases.py` injects a host change, insufficient identities, missed driver headroom and cancellation during a live seat and proves the invalid or interrupted cell cannot contribute a qualifying result.

## References

- `docs/design/agent-session-evaluation.md`, Workload and measurement protocol; Booking and running
