+++
id = "E2-89"
title = "Lifecycle agent: the program that runs its transitions and duties, as python -m rhi_lifecycle"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-37"]
+++

## Context

Issues E2-33 to E2-37 build the lifecycle agent's parts as modules: the release and the host
(E2-33), the launch transitions (E2-34), the serving duties and the metrics endpoint (E2-35), the
termination handling (E2-36) and the stage timings (E2-37). Something must run them in order on an
instance, or every launch hook waits out its timeout, three abandoned launches suspend the pool
(issue E2-08), and the deployed verification fails. This issue is that program. The agent's unit
runs it as `python -m rhi_lifecycle` (issue E2-70).

**What it does.** At start it reads its settings from its environment, which the agent's unit fills
from `/run/rhi/release.env` (the variables of issue E2-38), and starts the metrics endpoint of issue
E2-35. It then reads the instance's target lifecycle state from the instance metadata on a 5-second
tick: the design gives no interval for it, and 5 seconds is the heartbeat's own (section 4.3). A
launch state runs the matching launch transition of issue E2-34. Once a promotion, or a launch
straight into service, has completed its hook, the agent runs the serving duties of issue E2-35
(the heartbeat and the liveness check, and whatever later duties join them in `serving.py`, such
as the cache capacity of issue E2-66) until the target state leaves service; it then hands the
instance to the termination handling of issue E2-36, whether it is terminated or returns to the
warm pool, and stops its serving duties once the engine is drained. After an entry to the warm pool
it runs no serving duty: the group stops the instance.

**A restart of its unit.** The agent's unit restarts on failure (issue E2-70), and a restarted agent
must not run a launch transition on an instance that is already serving. So the agent records that
the instance has served since its start in `/run/rhi`, which is emptied at every stop, and a restart
that finds the record resumes the serving duties, or hands the instance to the termination handling
when its target state has left service.

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/__main__.py` is the agent's program, run as python -m rhi_lifecycle: it reads its settings from its environment, starts the metrics endpoint of issue E2-35, and reads the instance's target lifecycle state from the instance metadata every 5 seconds.
- [ ] It runs the launch transition of issue E2-34 that the target state calls for; after a promotion or a launch straight into service has completed its hook it runs the serving duties of issue E2-35 until the target state leaves service, then hands the instance to the termination handling of issue E2-36 and stops the serving duties once the engine is drained; after an entry to the warm pool it runs no serving duty.
- [ ] It records in /run/rhi that the instance has served since its start, and a program that starts and finds that record runs no launch transition: it resumes the serving duties while the target state is in service, and hands the instance to the termination handling otherwise.
- [ ] `tests/unit/lifecycle/test_main.py` runs the program against stand-ins for the instance metadata, Auto Scaling, the host module of issue E2-33, the engine, the heartbeat table, the state folder and the clock.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle/test_main.py` passes: a launch straight into service runs its transition, then the serving duties, and, once the target state reads Terminated, the termination handling, with the serving duties stopped once the engine is drained; an entry to the warm pool completes its hook and starts no serving duty.
- [ ] `tests/unit/lifecycle/test_main.py::test_a_restarted_agent_resumes_serving` starts the program with the record of having served and a target state in service, and finds the serving duties running and no launch transition run.

## References

- `docs/design/system-design.md`, sections 4.3, 5.2
