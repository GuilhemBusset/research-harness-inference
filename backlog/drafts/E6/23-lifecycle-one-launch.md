+++
id = "E6-23"
title = "Lifecycle agent: one launch, one departure"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-22"]
+++

## Context

The lifecycle agent on each instance completes the group's lifecycle hooks (system design, section
4.3). With a warm pool it told three launch cases apart by the instance's target state and by
whether the engine image was on disk: an entry to the warm pool, a promotion from it with a
deadline of its own, and a launch straight into service. It also recognised an instance that came
back from service to the warm pool, by a file it wrote when the instance first served. Since E6-22
no group has a warm pool, and two transitions are left:

| Transition | What the agent does | Timeout, and what follows |
| --- | --- | --- |
| An instance launches | Pull the image, check the release, start the engine, warm up, complete the hook | The launch hook's timeout. Then the instance is abandoned, and the group launches another |
| An instance leaves service | Wait for the engine to drain, complete the hook | The termination hook's timeout. Then the instance leaves |

The launch is bounded by the hook's own timeout, which `src/rhi/pools/hooks.py` sets as today: 15
minutes, or twice the measured launch when that is longer. The agent keeps no deadline of its own.
The check of the release at launch stays, and so do the stages a launch records and the heartbeat
row it writes at each.

The stage metrics carry a label for the transition, which the fleet dashboard may select on. They
keep the label, with the one value a launch into service has today, so that no dashboard has to
change with this issue.

The agent is part of each pool's release: this issue changes the release, and the instances of the
next session start on it.

After this issue the deployed agent handles a launch and a departure, and nothing else.

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/launch.py` has one launch: pull the image if the disk lacks it, check the release, start the engine, wait for its health route, send the warm-up request, and complete the launch hook, or abandon the instance when a step fails; it has no entry to a warm pool, no promotion, no deadline of its own, and no file that marks an instance as having served.
- [ ] `services/engine/lifecycle/rhi_lifecycle/termination.py` drains an instance that leaves service for good and knows no return to a warm pool, and `services/engine/lifecycle/rhi_lifecycle/__main__.py`, `services/engine/lifecycle/rhi_lifecycle/host.py` and `services/engine/lifecycle/rhi_lifecycle/release.py` handle and describe none.
- [ ] `services/engine/lifecycle/rhi_lifecycle/stages.py` has one transition, under the value a launch into service has today, and the two stage metrics keep their label for it.
- [ ] `src/rhi/pools/hooks.py` describes the launch hook as holding a launching instance, with the timeout it has today.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle tests/synth/test_pool_hooks.py` passes: a launch whose every step passes completes the hook, and one whose release differs or whose engine never answers abandons the instance.
- [ ] `git grep -n -i -e "warm pool" -e Warmed -e "Transition.PROMOTION" -e "Transition.ENTRY" -e SERVED_FILE -- services/engine/lifecycle src/rhi/pools/hooks.py` prints nothing.
- [ ] In the pipeline, `tests/smoke/test_smoke.py` passes on a mock instance that launched with this agent (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.3 and 5.2
- `services/engine/lifecycle/rhi_lifecycle/launch.py`
- `services/engine/lifecycle/rhi_lifecycle/stages.py`
- `src/rhi/pools/hooks.py`
