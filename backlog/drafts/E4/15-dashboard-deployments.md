+++
id = "E4-15"
title = "Dashboard: deployments, and the release each pool serves"
labels = ["size:S", "kind:app"]
epic = "E4"
blocked_by = ["E2-35", "E2-61", "E4-30"]
+++

## Context

One of the six dashboards of section 7.3: pipeline runs and their results, and the version in
service, from the pipeline's events in CloudWatch Logs and the engine's identity report in
Prometheus (section 7.2). The events are kept by issue E4-30. The identity report is the lifecycle
agent's: one metric whose labels are the image digest and the model revision being served, because
the engine does not know its image digest (section 4.1; issue E2-35).

A deployment does not reach an existing instance by itself; a release starts an instance refresh,
and the pipeline's verification asserts that the served release equals the deployed one (section
4.3). The release panel lets the admin see the same thing: which digest and revision each instance
serves, and when that changed.

## Deliverables

- [ ] `dashboards/rhi_dashboards/deployments.py` defines the deployments dashboard: the pipeline's executions with their result, from the log group of issue E4-30; and, per pool of the configuration, the image digest and model revision each instance serves over time, from the lifecycle agent's metric.
- [ ] `tests/unit/dashboards/test_deployments.py` builds the dashboard from the example configuration and checks its panels.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_deployments.py` passes: the release panel groups by the digest and the revision labels of the lifecycle agent's metric and by instance, and one panel reads the pipeline's executions from the log group of issue E4-30.
- [ ] `uv run --locked pytest tests/unit/dashboards/test_build.py` passes with the deployments dashboard in the package.

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 7.2, 7.3
