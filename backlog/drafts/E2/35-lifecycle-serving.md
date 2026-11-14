+++
id = "E2-35"
title = "Lifecycle agent: heartbeat every 5 seconds, the release it serves, and liveness by inference"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-03", "E2-33", "E2-82"]
+++

## Context

While an instance serves, its agent has two duties (section 4.3). It writes a heartbeat row every 5
seconds with the engine's running and waiting counts and the release it serves, which is the
scaler's view of engine load. And it runs a one-token completion every minute and restarts the
engine if that fails, because an engine can hang with its health route still answering (evidence
E23).

The identity row of the engine contract is the agent's too: it reports the image digest and model
revision being served, in its heartbeat and as a metric, because the engine itself does not know
its image digest (section 4.1). The pipeline's verification and the deployments dashboard read that
metric.

The agent's metrics are served on one endpoint, on localhost only: `127.0.0.1`, port 9110, route
`/metrics`, in the Prometheus text format, with prometheus-client of the lifecycle group (issue
E1-16). The design names
no port; 9110 is free of the engine's 8000 and of the usual exporters' 9100 and 9400. The node's
collector scrapes it (issue E2-58), and the stage timings and the cache capacity add their metrics to
it (issues E2-37 and E2-66). The heartbeat goes to the heartbeat table named in the settings of the
release parameter, and the engine is restarted through the host module (issue E2-33).

## Deliverables

- [ ] `services/engine/lifecycle/rhi_lifecycle/serving.py` reads the engine's running and waiting counts from its metrics on localhost, by the names of the per-engine table, and writes the instance heartbeat of issue E2-03 every 5 seconds, with the engine image digest and the model revision being served.
- [ ] `services/engine/lifecycle/rhi_lifecycle/metrics.py` serves the agent's metrics on 127.0.0.1, port 9110, route /metrics, and `services/engine/lifecycle/rhi_lifecycle/serving.py` exposes there one metric whose labels are the engine image digest and the model revision being served.
- [ ] Every minute it sends a one-token completion to the engine with the pool's secret, read from Secrets Manager by the ID the settings give; when it fails or times out, it restarts the engine's unit through the host module and records the restart.
- [ ] `tests/unit/lifecycle/test_serving.py` covers the duties against the mock engine, with stand-ins for the table, the secret and the host module.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/lifecycle/test_serving.py` passes: the heartbeat carries the counts the engine reports, the image digest and the model revision, every 5 seconds on a clock the test controls, and the metric carries the same digest and revision.
- [ ] `tests/unit/lifecycle/test_serving.py::test_a_hung_engine_is_restarted` puts the mock in the mode where inference hangs while health answers, and the agent restarts it within the minute, on a clock the test controls.

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 5.1
- `docs/design/evidence.md`, E23
