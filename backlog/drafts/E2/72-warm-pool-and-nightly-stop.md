+++
id = "E2-72"
title = "A pool's stopped warm pool, and the nightly action that sets its desired capacity to zero"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-71"]
+++

## Context

The warm pool keeps stopped instances, which cost only their disk and keep the pulled image.
`warm_instances` is the number of prepared instances, running or stopped, that the group keeps, with
a warm pool minimum of zero; a minimum above zero would launch a fresh instance to refill the pool
on every wake (section 4.2; evidence E10). A pool with `warm_instances = 0`, as the upgrade pool will
be, has no warm pool. Instances return to the warm pool on scale-in.

What stays outside the scaler, so that a broken scaler cannot cost money, includes a nightly
scheduled action that sets the desired capacity to zero, at 02:00 in the configured time zone by
default (sections 5.1 and 17). It sets neither the minimum nor the maximum, which the fleet-cap
invariant of issue E2-39 checks.

The hour is configuration: the key `nightly_stop`, a time of day, "02:00" by default (section 17),
read at synthesis; the time zone is the schedule's, which issue E1-03 introduced. Nothing at run time
reads it, so it reaches no service. The owner's configuration in SSM needs no new value.

## Deliverables

- [ ] `src/rhi/pools/group.py` gives a pool whose warm_instances is above 0 a warm pool of stopped instances, with a prepared capacity equal to warm_instances and a minimum of 0, that takes instances back on scale-in; a pool whose warm_instances is 0 has none.
- [ ] A scheduled action sets the group's desired capacity to 0, and neither its minimum nor its maximum, every night at the time of the key nightly_stop in the configuration's time zone.
- [ ] `src/rhi/config.py` reads the optional key nightly_stop, a time of day in hours and minutes, 02:00 by default, refusing any other form; `config.example.toml` gives it 02:00, and `tests/unit/test_config.py` covers its default and a refusal.
- [ ] Every cdk-nag finding `src/rhi/pools/group.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_group.toml`.
- [ ] `tests/synth/test_pool_group.py` checks the warm pool and the nightly action of the mock pool.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_group.py tests/policy/test_fleet_cap.py` passes on the app with the example configuration's mock pool: a warm pool preparing 1 stopped instance with a minimum of 0 that takes instances back on scale-in, and a nightly action at 02:00 in the example's time zone that sets the desired capacity alone.
- [ ] `tests/synth/test_pool_group.py::test_no_warm_pool_at_zero` shows a pool configured with warm_instances 0 synthesizing with no warm pool.
- [ ] `uv run --locked pytest tests/unit/test_config.py` passes: a configuration without nightly_stop reads 02:00, and 2am is refused naming the key.

## References

- `docs/design/system-design.md`, sections 4.2, 5.1, 17
- `docs/design/evidence.md`, E10
