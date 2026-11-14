+++
id = "E2-09"
title = "The scaler's scenarios, played against a simulated pool"
labels = ["size:M", "kind:test"]
epic = "E2"
blocked_by = ["E2-08"]
+++

## Context

Section 5.1 names the situations in which a pool's size is at risk of a race: two first requests at
once, a request arriving during scale-in, a wake after a long idle, a launch that never becomes
healthy, a gateway that stops reporting. Each is a sequence over time, not one call of the decision
function. This issue plays them: a simulated clock, a simulated group whose instances take time to
start and can fail, and gateways that report.

Phase 1 ends when these scenarios pass in the pipeline's verification (section 14).

## Deliverables

- [ ] `tests/unit/scaler/sim.py` simulates time, a group (instances that start after a set delay, can be abandoned, and return to a warm pool), gateway heartbeats and instance heartbeats, and applies each decision of the function to the group.
- [ ] `tests/unit/scaler/test_scenarios.py` plays the five scenarios of section 5.1, each as a named test.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_scenarios.py` passes: two first requests at once start one instance; a request that arrives during a scale-in is served and its instance is not the one released; a wake after a long idle starts one instance.
- [ ] `tests/unit/scaler/test_scenarios.py::test_a_launch_that_never_becomes_healthy` ends with launches suspended after three attempts, and `tests/unit/scaler/test_scenarios.py::test_a_gateway_that_stops_reporting` ends at zero after 10 minutes.

## References

- `docs/design/system-design.md`, sections 5.1, 14
