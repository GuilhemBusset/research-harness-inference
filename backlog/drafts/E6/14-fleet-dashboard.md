+++
id = "E6-14"
title = "Fleet dashboard: instances requested against instances available, and each instance's telemetry"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-03"]
+++

## Context

The instructor wants to see, in Grafana, the GPUs a session requested and those available, beside
each machine's telemetry (system design, requirement R5 and section 7.3). The scaler publishes what
is needed for each pool since E6-03 (section 7.2): the session's window and whether it is active;
the instances requested, asked of AWS, waiting for capacity, starting, in service and draining; the
seconds the pool has waited; the cause of the last failed launch. Each machine's own telemetry
comes from the machine, labelled by the node's collector with the pool and the instance: the DCGM
series of the GPU dashboard and the engine's series of the per-engine table.

Three things in the dashboards rest on what goes away, and move here, before the series they read
are removed (E6-16, E6-22):

- **The session gauge.** The objectives are read only while a session is on, by one gauge without
  a label, `rhi_scaler_session_on` (`rhi_dashboards.rules`). Sessions are per pool: each pool's
  objectives are read while that pool's session is active, by `rhi_scaler_session_active`, and the
  gateway's objective, which is about no pool, while any pool's session is active.
- **The fifth objective**, the start from the warm pool under five minutes. The design has four
  objectives (section 7.3): nobody waits for a launch behind a request, and there is no warm pool.
- **"Asleep".** A pool with no instance has no engine series, and its panels show the pool's state
  from the scaler, never a gap or a stale value. The state is now one of two: "no session" while
  the pool's session is not active, "waiting for capacity" while it is and nothing is in service.

The dashboards are held to the rules of `rhi_dashboards.check`: every series a panel reads is
declared with the file that emits it.

After this issue the deployed Grafana shows the new fleet row. The scaler still sends the two
series the dashboards no longer read.

## Deliverables

- [ ] `dashboards/rhi_dashboards/fleet.py` shows, in each pool's row: over time, the instances requested, asked of AWS, waiting for capacity, starting and in service; the session's start and end and whether it is active; the seconds the pool has waited and the cause of the last failed launch; the scaler's decisions by reason; and the seconds of each stage of each launch.
- [ ] Under those panels the row has a table with one line per instance of the pool: GPU utilization, GPU memory used and power, the engine's running and waiting requests, its output tokens per second and its time to first token; the mock pool's table has the engine's columns only.
- [ ] `dashboards/rhi_dashboards/rules.py` reads a pool's session from the gauge rhi_scaler_session_active with the pool's label; an objective of a pool is read while that pool's session is active, and the gateway's objective while any pool's is.
- [ ] `dashboards/rhi_dashboards/objectives.py` holds the four objectives of section 7.3 and no objective on the time a launch takes.
- [ ] A panel of engine or GPU series, in `dashboards/rhi_dashboards/gpu.py`, `dashboards/rhi_dashboards/inference.py` and the fleet row, reads "no session" while the pool's session is not active and "waiting for capacity" while it is active and no instance is in service.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards tests/synth/test_grafana_dashboards.py` passes: every dashboard is built and passes the rules of the check, and the fleet row of each pool holds the panels and the table above.
- [ ] `git grep -n -e rhi_scaler_session_on -e rhi_scaler_warm_instances -- dashboards` prints nothing.
- [ ] In Grafana, during a session on the coursework pool, the fleet row shows the instances requested and in service and one line per instance with its GPU and engine telemetry (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 7.2 and 7.3
- `dashboards/rhi_dashboards/fleet.py`
- `dashboards/rhi_dashboards/rules.py`
- `dashboards/rhi_dashboards/objectives.py`
- `services/scaler/rhi_scaler/metrics.py`
