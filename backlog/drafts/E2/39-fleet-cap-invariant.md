+++
id = "E2-39"
title = "The fleet-cap invariant: a pool's group has a minimum of zero, no desired capacity and its pool's maximum"
labels = ["size:S", "kind:test"]
epic = "E2"
blocked_by = ["E1-07", "E2-04"]
+++

## Context

The fleet cap is one of the invariants of section 8.4. A pool becomes one Auto Scaling group
(section 4.2), and what stays outside the scaler, so that a broken scaler cannot cost money, starts
with the group's maximum size (section 5.1). The group's minimum is always zero and it has no scaling
policy and no scheduled minimum: schedules, ceilings and stops all live in the scaler, so they cannot
contradict one another (section 5.1). The scaler is the only writer of the desired capacity, so the
template sets none: a desired capacity in the template would reset the pool at every deployment, a
second writer.

The invariant comes before the construct it constrains (issue E2-71 builds the groups), so it passes
on the app as it stands and is proven on templates built in the test. It finds a group's pool by the
tag `rhi-pool` that issue E2-71 puts on every group, and the pool's `max_instances` in the
configuration the policy tests synthesize (issue E1-07). A group without that tag, or whose tag names
no configured pool, fails: every group of the stage is a pool's.

This issue creates `tests/policy/test_fleet_cap.py`. `tests/policy/` is a protected path (ADR 0001,
decision 10): the builder may write the file named below because this issue names it.

## Deliverables

- [ ] `tests/policy/test_fleet_cap.py` fails when an Auto Scaling group of the stage has a minimum above 0, a desired capacity in the template, a scaling policy, a scheduled action that sets its minimum or its maximum, or a maximum other than the max_instances of the pool its tag rhi-pool names.
- [ ] It fails when a group carries no tag rhi-pool, or one that names no configured pool.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/policy/test_fleet_cap.py` passes on the app as it stands.
- [ ] `tests/policy/test_fleet_cap.py::test_detects_a_raised_cap` fails a template, built in the test, whose group has a larger maximum than its pool allows, and `tests/policy/test_fleet_cap.py::test_detects_a_desired_capacity` fails one whose group sets a desired capacity.
- [ ] `tests/policy/test_fleet_cap.py::test_detects_a_scheduled_minimum` fails a template, built in the test, with a scheduled action that sets a minimum of 1, and `tests/policy/test_fleet_cap.py::test_detects_an_untagged_group` fails one whose group has no tag rhi-pool.

## References

- `docs/design/system-design.md`, sections 4.2, 5.1, 8.4
