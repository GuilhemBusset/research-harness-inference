+++
id = "E4-11"
title = "Dashboard: GPU health"
labels = ["size:S", "kind:app"]
epic = "E4"
blocked_by = ["E2-61", "E3-05", "E3-10"]
+++

## Context

One of the six dashboards of section 7.3. Its numbers come from the NVIDIA DCGM exporter, pinned at
4.9 or later: that release renamed the default metrics and changed their units, with utilization as
a ratio, memory in bytes, energy in joules and power in watts; an image of 4.8 still emits the old
names (section 7.2; evidence E19). The exporter runs on every GPU instance with the fields listed in
its counters file, `services/engine/dcgm/counters.csv`, and the node's collector adds the pool,
instance and instance-type labels (issues E3-05 and E2-58).

Utilization of memory bandwidth matters most here: a stream's speed is bounded by memory traffic,
since each step of the engine reads the weights once and the cached transcript of every stream
(section 4.7). A pool whose engine is the mock has no GPU and no row; the example configuration's
GPU pool is the coursework pool (issue E3-10).

It follows the rules and the build of issue E2-61: rates per instance then summed, "asleep" for a
pool at zero.

## Deliverables

- [ ] `dashboards/rhi_dashboards/gpu.py` defines the GPU health dashboard with one row per pool of the configuration whose engine is not the mock, and per instance: utilization of memory bandwidth and of compute, memory used, temperature, power, the rate of energy used, and Xid errors.
- [ ] It declares each DCGM metric name it queries as coming from the exporter's counters file of issue E3-05, which names each one.
- [ ] `tests/unit/dashboards/test_gpu.py` builds the dashboard from the example configuration and checks its panels.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_gpu.py` passes: the seven quantities each have a panel in every GPU pool's row, the mock pool has no row, and every DCGM name the dashboard queries is a field of the exporter's counters file.
- [ ] `uv run --locked pytest tests/unit/dashboards/test_build.py` passes with the GPU health dashboard in the package.

## References

- `docs/design/system-design.md`, sections 4.7, 7.2, 7.3
- `docs/design/evidence.md`, E19
- `services/engine/dcgm/counters.csv`, delivered by issue E3-05
