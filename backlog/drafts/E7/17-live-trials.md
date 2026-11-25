+++
id = "E7-17"
title = "Run and grade coursework at low load and under class contention"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-05", "E7-09", "E7-10", "E7-11", "E7-12", "E7-16"]
+++

## Context

One seat runs the real harness and the remaining seats replay the applicable class shape. Graders and task inputs are the same at both loads.

## Deliverables

- [ ] `tools/evaluate/trials.py` executes the planned instances with their harness adapters and independent graders, substituting exactly one live seat and keeping other seats active until that trial ends.
- [ ] It records completed successes, measured failures, invalid trials, turns, fan-out, retries, output usage and task duration including tools, and releases all workers and background traffic after each attempt.
- [ ] `tools/evaluate/tests/test_trials.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_trials.py` drives a hands-on task and a parallel task at low and class load, verifies seven background seats for an eight-seat trial and avoids duplicating the single-seat condition.
- [ ] `tools/evaluate/tests/test_trials.py` shows a wrong artifact, timeout or endpoint refusal counts as a failed attempt, a broken grader remains incomplete, and cancellation stops all background work.

## References

- `docs/design/agent-session-evaluation.md`, Course quality and the grader; Workload and measurement protocol
