+++
id = "E3-39"
title = "A canary instance outside any group: the lifecycle agent reports on the console, and the role the promotion job may pass"
labels = ["size:M", "kind:app"]
epic = "E3"
blocked_by = ["E2-34", "E2-35", "E2-37", "E2-41", "E2-71", "E2-89"]
+++

## Context

The promotion job of issue E3-02 tries a candidate machine image by starting one instance from the
pool's launch template, outside the pool's group, so that the scaler, which only knows the group's
instances and the heartbeats of the instance table, never counts it (sections 4.3 and 5.1). Every
instance runs the same boot unit and lifecycle agent (issue E2-38), so the agent has to recognise
such an instance and behave differently on it.

An instance of a group finds its target lifecycle state in the instance metadata; an instance that
belongs to no group finds none. The agent's program reads that state every 5 seconds and runs the
matching transition (issue E2-89); this issue adds its one other branch. On such an instance the agent does what a launch straight into
service does (issue E2-34): pull the engine image, check the release, start the engine, send the
real warm-up request. It then completes no lifecycle hook, since none exists; writes no heartbeat
row, so that nothing reads it as one of the pool's instances; and writes its outcome as one line on
the instance's serial console, which the promotion job reads with EC2's console output. The console
needs no permission on the instance role and leaves the stage's tables untouched. The line holds
only names and values of this project: the outcome (served, or failed with the last stage reached,
as the stages of issue E2-37 name it), and the image digest and model revision of the release.

The canary is started from the pool's launch template, so it carries the tags rhi-pool and
rhi-release-parameter like the group's instances (issue E2-71), and its boot unit fetches and checks
the pool's current release as on any start (issue E2-38). The agent runs in a container and acts on
the host through its host module (issue E2-33); it writes the console line the same way.

The job starts the instance with the pool's instance role, which it must pass. So that the job's
role may pass the GPU pools' roles and no other, those roles live under one IAM path.

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/canary.py`, on an instance whose metadata gives no Auto Scaling lifecycle state, pulls the engine image, checks the release, starts the engine and sends the warm-up request, as for a launch straight into service, and completes no hook.
- [ ] On such an instance the agent writes no heartbeat row and starts none of its serving duties, and writes one line of a fixed form on the serial console: served with the release's image digest and model revision, or failed with the last stage reached.
- [ ] The agent's program, `services/engine/lifecycle/rhi_lifecycle/__main__.py` of issue E2-89, hands an instance whose metadata gives no lifecycle state to the canary module and every other instance to its launch transitions as before.
- [ ] `src/rhi/pools/role.py` places every GPU pool's instance role under one IAM path, a constant of the module, and the mock pool's role outside it.
- [ ] Every cdk-nag finding `src/rhi/pools/role.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_role.toml`.
- [ ] `tests/unit/lifecycle/test_canary.py` covers the canary with stand-ins for the metadata, the container runtime, the engine, the heartbeat table and the console, `tests/unit/lifecycle/test_main.py` the program's choice, and `tests/synth/test_pool_role.py` checks the path.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle/test_canary.py` passes: with no lifecycle state, the console line says served after the warm-up request is answered, and says failed with the last stage the stand-ins let it reach when the engine never becomes healthy; in both cases no hook call and no heartbeat write is made.
- [ ] `uv run --locked pytest tests/unit/lifecycle/test_main.py tests/unit/lifecycle/test_launch.py tests/unit/lifecycle/test_serving.py` passes: the program hands an instance with no lifecycle state to the canary module, and an instance with a lifecycle state behaves as before.
- [ ] `uv run --locked pytest tests/synth/test_pool_role.py` passes: a GPU pool's role of a configuration built in the test is under the module's path, and the mock pool's is not.

## References

- `docs/design/system-design.md`, sections 4.3, 5.1
- `docs/design/evidence.md`, E10
