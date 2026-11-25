+++
id = "E6-06"
title = "Pools: no nightly stop"
labels = ["size:M", "kind:infra"]
epic = "E6"
+++

## Context

Each pool's group carries a scheduled action that sets its desired capacity to zero every night, at
the configuration's `nightly_stop`. With sessions it goes (system design, section 5.1): a session
may cross any hour, a class booked the evening before or a measurement that runs overnight, and a
stop at a fixed hour would take from it the instances still starting. It could not take those in
service at all, since they are protected from scale-in (`docs/design/evidence.md`, E32). What ends
a forgotten session is its own end.

The group then has no scaling policy and no scheduled action of any kind: every rule about a pool's
size lives in the scaler.

**This issue changes an invariant.** `tests/policy/test_fleet_cap.py` refuses a scheduled action
that sets a minimum or a maximum, and lets one through that sets only a desired capacity, because
the nightly stop was one. With no nightly stop, the invariant becomes: a pool's group has no
scheduled action. `tests/policy/` is a protected path (ADR 0001, decision 10); this issue names the
files it changes there, and the change tightens the rule.

The key `nightly_stop` is retired, not removed: the deployed configuration still holds it, and the
loader fails closed on a key it does not know. A retired key is accepted, checked as before and
read into nothing, as the key `session_windows` is today. `time_zone` stays: the session commands
read their times in it.

After this issue the deployed groups have no scheduled action; nothing else changes.

## Deliverables

- [ ] `src/rhi/pools/group.py` creates no scheduled action on a pool's group, and `src/rhi/pools/stack.py` no longer gives it a time of day or a time zone.
- [ ] `src/rhi/config.py` holds `nightly_stop` as a retired key: a configuration that gives it a time of day still loads, any other value is refused, and the loaded configuration has no such field; `config.example.toml` no longer holds the key.
- [ ] `tests/policy/test_fleet_cap.py` refuses every scheduled action on a pool's group, whatever it sets.
- [ ] `tests/policy/acknowledged/pools_group.toml` no longer speaks of a nightly scheduled action.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_group.py` passes: the pools stack of the example configuration holds no resource of the type AWS::AutoScaling::ScheduledAction.
- [ ] `tests/policy/test_fleet_cap.py::test_detects_a_scheduled_action` fails a template, built in the test, whose group has a scheduled action that sets only a desired capacity.
- [ ] `uv run --locked pytest tests/policy tests/unit/test_config.py` passes: a configuration that still gives `nightly_stop` as 02:00 loads, and one that gives it as text that is no time of day is refused by the key's name.
- [ ] `scripts/check.sh synth` exits 0.

## References

- `docs/design/system-design.md`, sections 5.1 and 17
- `docs/design/evidence.md`, E32
- `src/rhi/pools/group.py`
- `tests/policy/test_fleet_cap.py`
