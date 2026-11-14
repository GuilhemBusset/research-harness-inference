+++
id = "E2-37"
title = "Lifecycle agent: time every stage of a start"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-35", "E2-36"]
+++

## Context

Cold-start time is the central engineering number of the design, and nobody has published it for a
stopped GPU instance (sections 0 and 5.2). The lifecycle agent sees every stage of a start, so it
measures them: the cold-start panel of section 7.2 and spike S1 both read these numbers, and the
reporter's failure report names the last stage an instance reached (section 8.4).

The stages are marked where they happen, in the launch transitions (issues E2-34 and E2-36, both in
`launch.py`), and reported on the agent's metrics endpoint and in its heartbeat (issue E2-35).

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/stages.py` names the stages of a start as an enumeration (boot, release check, image, container start, weights, warm-up, hook) and records the seconds each took, per launch and per transition.
- [ ] `services/engine/lifecycle/rhi_lifecycle/launch.py` marks the start and the end of each stage of a launch, and the agent exposes the durations on its metrics endpoint of issue E2-35 and puts the last stage reached in the instance's heartbeat.
- [ ] `tests/unit/lifecycle/test_stages.py` covers the timings on a clock the test controls.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle/test_stages.py` passes: a simulated promotion gives one duration per stage that sum to the whole, and an abandoned launch leaves its last stage in the heartbeat.

## References

- `docs/design/system-design.md`, sections 5.2, 7.2, 8.4
