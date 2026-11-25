+++
id = "E6-02"
title = "Scaler: a pool is never below what its session asks"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-01"]
+++

## Context

The scaler will set each pool to what its session asks and to nothing else (system design, section
5.1). This issue is the first step, and a bridge: the scaler reads each pool's session and treats it
as a floor under the rules it has today. The rules on demand, load and idleness stay as they are
for a pool with no session, so the verification, the canary and the spikes, which still wake a pool
through the gateway, keep working until each has moved to sessions. E6-15 then removes those rules.

What the deployed system does after this issue: a pool whose session is active comes up to the
session's number of instances, capped at `max_instances`, and stays there until the session ends.
After the end the existing rules take it down, the idle rule after `idle_minutes`. A pool with no
session behaves as before. Lowering a session's number while it is active releases nothing by
itself: the rules of today decide, above the new number, and the release order of section 5.1
comes with E6-15.

The decision stays a pure function of its inputs. The session reaches it through the pool's inputs,
and the bounds are those of `rhi_common.sessions`, which needs the longest session: the construct
gives it to the function in its settings.

The design does not say what a run does with a session item it cannot read. This issue asks for no
rule about it.

## Deliverables

- [ ] `src/rhi/pools/scaler.py` writes the configuration's `session_max_hours` into the function's settings, and `services/scaler/rhi_scaler/settings.py` reads it, 12 when the settings hold none.
- [ ] `services/scaler/rhi_scaler/aws_io.py` reads each pool's session item on every run and gives the session to the pool's inputs; a pool with no item has no session.
- [ ] `services/scaler/rhi_scaler/model.py` carries, on each pool's decision, `requested`: what the pool's session asks for at the time of the run, by the function `requested` of the shared sessions module.
- [ ] In `services/scaler/rhi_scaler/decide.py`, when `requested` is above the desired capacity the other rules give, the desired capacity is `requested` and the decision's reason is `provision`, unless the pool's launches are suspended.
- [ ] While `requested` is above zero, a rule that would set the desired capacity below `requested` does not apply: the capacity stays, and no instance's scale-in protection is lifted.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_session_floor.py` passes: a session of 2 on a pool at zero with no demand gives a desired capacity of 2 with the reason `provision`, and the idle rule, the stop of a disabled pool and a silent gateway each leave it at 2 while the session is active.
- [ ] `tests/unit/scaler/test_session_floor.py::test_after_the_end_the_other_rules_apply` shows the pool going to zero by the idle rule once the session has ended, and `tests/unit/scaler/test_session_floor.py::test_a_suspended_pool_is_not_raised` that a session does not raise a pool whose launches are suspended.
- [ ] `uv run --locked pytest tests/unit/scaler tests/synth/test_scaler.py` passes: every existing scenario of a pool with no session decides as before, and the function's settings hold `session_max_hours`.

## References

- `docs/design/system-design.md`, sections 5.1 and 5.6
- `services/scaler/rhi_scaler/decide.py`
- `services/common/rhi_common/sessions.py`, delivered by E6-01
