+++
id = "E7-15"
title = "Forward student requests with per-trial identity and shared limits"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-04"]
+++

## Context

The trial has a local endpoint. Only the operator-side forwarder holds the canary key and targets the selected gateway and pool.

## Deliverables

- [ ] `tools/evaluate/forwarding.py` streams all three harness wire formats through the fixed gateway and pool, injects the student identity outside the workspace and emits typed public measurements for live traffic, side calls and retries.
- [ ] The forwarder and supervisor enforce the plan’s 12-minute, 32-turn and 32,000 generated-token limits across the entire agent tree including concurrent calls, with bounded cancellation and fixed failure evidence.
- [ ] `tools/evaluate/tests/test_forwarding.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_forwarding.py` uses local transports to verify route and model selection, retry and side-call accounting, stream completion and the absence of secrets or bodies from trial-visible state and evidence.
- [ ] `tools/evaluate/tests/test_forwarding.py` exercises two simultaneous child requests near the aggregate limits, missing usage, an expired deadline and a client disconnect without leaking a reservation or allowing an over-limit trial to pass.

## References

- `docs/design/agent-session-evaluation.md`, Course quality and the grader; Reading a result
- `spikes/forwarder.py`
