+++
id = "E6-22"
title = "Pools: no warm pool"
labels = ["size:M", "kind:infra"]
epic = "E6"
blocked_by = ["E6-03"]
+++

## Context

Each pool's group keeps stopped instances in a warm pool, so that a start need not pull the engine
image. The design drops it (system design, section 4.5). A stopped instance holds no hardware: it
can start only in its own zone and as its own type, fails when that has no capacity, and is then
terminated and replaced by a new launch. In the account, five starts of five failed that way
(`docs/design/evidence.md`, E32). It narrows the search for capacity when capacity is the
constraint, to save minutes nobody waits for once sessions are booked ahead, and it costs its disk.

This issue removes the warm pool from the group and from everything that reads it:

- the group's warm pool, and the warm pool's metrics among those the group publishes;
- the scaler's read of the warm pool, the action its role holds for it, the count of warm
  instances in its supply, its log and its gauge;
- the warm pool's term in the alarm on instances running with nothing in service, and in the cost
  dashboard's count of running instances.

A pool's entry still gives `warm_instances` until E6-21 retires the field: the group no longer
reads it, whatever it says. The lifecycle agent still knows the warm pool's two transitions, which
can no longer happen; E6-23 removes them. If the fleet dashboard still draws the warm gauge when
this issue is built, it stops here; E6-14 may already have removed it.

`tests/policy/` is a protected path (ADR 0001, decision 10). This issue names the two
acknowledgement files it changes there: both speak of the warm pool, and one acknowledges the
action the scaler's role loses.

After this issue the deployment deletes each group's warm pool, which terminates its stopped
instances. Every start is the launch of a new instance, in any zone and of any type the pool lists.

## Deliverables

- [ ] `src/rhi/pools/group.py` creates no warm pool for any pool and asks for none of the warm pool's group metrics, and `src/rhi/pools/stack.py` speaks of none.
- [ ] `services/scaler/rhi_scaler/aws_io.py` does not read a warm pool, the supply of `services/scaler/rhi_scaler/model.py` holds no warm instance, and `src/rhi/pools/scaler.py` gives the scaler's role no action to describe one.
- [ ] `services/scaler/rhi_scaler/metrics.py` sends no gauge of warm instances, `services/scaler/rhi_scaler/handler.py` logs none, and neither `dashboards/rhi_dashboards/fleet.py` nor `dashboards/rhi_dashboards/cost.py` reads that gauge or a warm pool's group metric.
- [ ] `src/rhi/guardrails/alarms_fleet.py` reads, for the alarm on instances running with nothing in service, the group's total instances and no metric of a warm pool.
- [ ] `tests/policy/acknowledged/pools_scaler.toml` and `tests/policy/acknowledged/pools_group.toml` acknowledge nothing about a warm pool.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_group.py tests/synth/test_coursework_pool.py` passes: the pools stack of the example configuration, whose coursework pool still gives two warm instances, holds no resource of the type AWS::AutoScaling::WarmPool.
- [ ] `uv run --locked pytest tests/unit/scaler tests/synth/test_scaler.py tests/synth/test_alarms_fleet.py tests/unit/dashboards tests/policy` passes.
- [ ] `git grep -n -i -e "warm pool" -e WarmPool -e warm_instances -- src/rhi/pools/group.py src/rhi/pools/scaler.py src/rhi/pools/stack.py src/rhi/guardrails services/scaler dashboards/rhi_dashboards/fleet.py dashboards/rhi_dashboards/cost.py` prints nothing.

## References

- `docs/design/system-design.md`, sections 4.5 and 5.2
- `docs/design/evidence.md`, E32
- `src/rhi/pools/group.py`
- `services/scaler/rhi_scaler/aws_io.py`
- `src/rhi/guardrails/alarms_fleet.py`
