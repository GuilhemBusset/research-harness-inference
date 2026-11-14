+++
id = "E5-02"
title = "Lifecycle agent: heartbeats extend the launch hook while a multi-GPU instance starts"
labels = ["size:M", "kind:app"]
epic = "E5"
blocked_by = ["E2-34", "E2-37", "E2-89", "E5-06"]
+++

## Context

A promotion to service is abandoned at twice the pool's measured start, and heartbeats extend that
deadline for the multi-GPU pools (section 4.3). The upgrade pool loads 186 GB across four GPUs,
which takes minutes at best (section 5.2), and it keeps no warm instance, so each of its starts is a
launch straight into service with the image to pull and the engine's compile cache empty. A fixed
deadline for such a start either abandons a slow start that is still moving or lets a stuck one
bill for long.

The agent knows the number of GPUs a pool's engine spans from the pool's release: issue E5-06 writes
it as the settings key gpus of the release (issue E2-33) for a pool that spans more than one, and
writes no such key for the others. Settings are outside the release's comparison, so reading the key
changes no instance's verdict; a release without it reads 1.

So on a pool whose engine spans more than one GPU (issue E5-06), the agent records a heartbeat on
the launch hook each time a stage of the start completes (the stages of issue E2-37). A heartbeat
restarts the hook's own timeout (issue E2-40), and the agent's own deadline moves with it. A start
then runs as long as each stage completes within one timeout of the previous one. An agent that
dies sends no heartbeat, and the hook abandons the instance one timeout later. Without a measured
start in configuration the agent keeps no deadline of its own (issue E2-34), and the hook's timeout
alone bounds each stage. The instance role may already extend its group's lifecycle actions
(issue E2-41).

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/release.py` reads the release's settings key gpus, 1 when the release does not give it, and accepts a release that gives it without changing the comparison.
- [ ] `services/engine/lifecycle/rhi_lifecycle/launch.py` records a heartbeat on the launch hook each time a stage of a launch into service completes, for a pool whose engine spans more than one GPU, and moves the transition's own deadline to twice the pool's measured start after that heartbeat.
- [ ] It records no heartbeat once the engine's process has exited, and abandons the instance then, as issue E2-34 does for a failed step.
- [ ] A pool whose engine spans one GPU gets no heartbeat, and its deadline stays the one of issue E2-34.
- [ ] `tests/unit/lifecycle/test_launch_heartbeats.py` covers the heartbeats with stand-ins for Auto Scaling, the engine's process and the clock, and `tests/unit/lifecycle/test_release.py` the key gpus.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle/test_launch_heartbeats.py` passes: on a clock the test controls, a four-GPU pool with a measured start of 600 seconds completes a start whose stages take 500 seconds each, with one heartbeat after each completed stage, and abandons a start whose weights stage has run 1,200 seconds since the last heartbeat.
- [ ] `tests/unit/lifecycle/test_launch_heartbeats.py::test_no_heartbeat_without_progress` shows no heartbeat for a one-GPU pool, and none after the engine's process exits, which abandons the instance at once.
- [ ] `uv run --locked pytest tests/unit/lifecycle/test_release.py` passes: a release with the key gpus 4 reads 4 and compares equal to the same release without it, and a release without it reads 1.

## References

- `docs/design/system-design.md`, sections 4.3, 5.2
- `docs/design/evidence.md`, E10
