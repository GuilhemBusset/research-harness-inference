+++
id = "E2-34"
title = "Lifecycle agent: the two launch transitions, each completed or abandoned"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-33"]
+++

## Context

Without explicit handling a lifecycle hook waits an hour, and a dead agent bills an hour of GPU per
attempt (section 4.3). Two launch transitions share one wait state, so the agent chooses by
transition (evidence E10). A new instance entering the warm pool pulls the image, checks the release
and completes the hook; the group then stops it. A warm instance promoted to service checks the
release, starts the engine, warms it up with a real request, and only then completes the hook.
A third case uses the same hook: an instance launched straight into service, which is a cold launch
when the warm pool is empty or its instance failed to start (section 5.2), and the only start of a
pool that keeps no warm instance, such as the upgrade pool of phase 4. It does both: it pulls the
image, then does everything a promotion does.

One launch hook serves both transitions, so the hook carries the longer timeout and the agent keeps
each transition's own deadline, abandoning the instance when it passes: 15 minutes for an entry to
the warm pool, twice the measured start for a promotion (section 4.3). The design gives the 15
minutes for the coursework pool; the mock pool uses the same. Until spike S1 has measured a start,
the hook's own timeout, 15 minutes, is the only bound on a promotion (section 17). The hook itself
is issue E2-40, and the measured start reaches the instance in the release parameter (issue
E2-33). The multi-GPU pools extend the hook by heartbeats, in phase 4.

The agent learns its transition from the instance metadata's target lifecycle state, and completes a
hook with the group and the hook names of the settings in the release parameter (issue E2-33). It
pulls the image and starts the engine through the host module of issue E2-33, never otherwise.

The same wait state also serves an instance that returns from service to the warm pool (evidence
E10). That one is draining, not entering: an instance that has served since its start is handled by
issue E2-36, never as an entry.

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/launch.py` reads which transition the instance is in from the instance metadata, and for an entry to the warm pool by an instance that has not served since its start pulls the engine image, checks the release and completes the launch hook named in the settings.
- [ ] For a promotion to service it checks the release, starts the engine, waits for its health route, sends one real warm-up request, and completes the hook only when that request is answered.
- [ ] For an instance launched straight into service, it pulls the engine image when the disk does not hold it, then does every step of a promotion, under the hook's own timeout.
- [ ] On a release that differs, on a step that fails, or at the transition's own deadline, it completes the hook with the result that abandons the instance.
- [ ] The deadline of an entry to the warm pool is 15 minutes, and that of a promotion is twice the pool's measured start read from the release parameter; while the parameter gives no measured start, the agent sets no deadline of its own on a promotion.
- [ ] `tests/unit/lifecycle/test_launch.py` covers both transitions with stand-ins for the instance metadata, Auto Scaling, the host module of issue E2-33 and the engine.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle/test_launch.py` passes: each transition completes its hook after its own steps, in order, and never before the warm-up request is answered on a promotion.
- [ ] `tests/unit/lifecycle/test_launch.py::test_a_launch_straight_into_service` pulls the image, starts the engine and completes the hook only after the warm-up request is answered, for an instance that never entered the warm pool.
- [ ] `tests/unit/lifecycle/test_launch.py::test_failures_abandon` shows an abandon for a differing release, a failed image pull, an engine that never becomes healthy and a warm-up request that fails.
- [ ] `tests/unit/lifecycle/test_launch.py::test_each_transition_keeps_its_own_deadline` abandons an entry to the warm pool at 15 minutes and, for a pool with a measured start of 200 seconds, a promotion at 400 seconds, on a clock the test controls.

## References

- `docs/design/system-design.md`, sections 4.3, 17
- `docs/design/evidence.md`, E10
