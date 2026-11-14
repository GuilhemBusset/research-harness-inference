+++
id = "E2-58"
title = "Collector configurations: each node and each gateway task pushes its own metrics, labelled by where they come from, and each node its logs"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-35"]
+++

## Context

On each instance an OpenTelemetry collector scrapes the engine, the host and the lifecycle agent on
localhost and pushes to Managed Prometheus with signed remote write; the gateway has a collector
beside it in its task (sections 4.3 and 7.1). The engine's metrics route is on localhost only
(section 4.1), so the collector is the only way those numbers leave the instance. GPU pools add the
DCGM exporter to the same scrape in phase 2 (issue E3-05).

**Ports.** The design names only the engine's port 8000. The engine's metrics are at port 8000 on
localhost, the lifecycle agent's at localhost port 9110, which issue E2-35 settles, and, in the
gateway's task, the gateway's at localhost port 9091, which this issue settles and issue E2-79
serves and checks against this configuration.

vLLM labels its series with the model's name only (evidence E3). The collector adds the labels for
pool, instance and instance type (section 7.2, sixth rule), so that every panel can take rates per
instance and then sum them: an instance's counters restart from zero every time it starts, and a
rate over a sum would read that as a fall (section 7.2, first rule). For the same reason the series
of two gateway tasks must never merge into one. The label names are pool, instance and
instance_type; the dashboards (issue E2-61) query them by those names.

The pool's name, the workspace's remote-write address, the region and the pool's log group reach
the instance's collector through its environment, as `RHI_POOL`, `RHI_WORKSPACE_URL`, `RHI_REGION`
and `RHI_LOG_GROUP`, which the collector's unit sets (issue E2-70); the gateway's sidecar gets the
same names from its task (issue E2-30). The instance's ID and
type come from the instance metadata, which requires IMDSv2 (section 4.3). The gateway task's ID
comes from the task metadata ECS gives the container.

All components log to CloudWatch Logs (section 7.1). The gateway's task logs through its own log
group (issue E2-30); on an instance, the same collector ships the logs of the instance's units (the
boot unit, the engine, the lifecycle agent, the collector itself and, on GPU pools, the DCGM
exporter) to the pool's log group, which issue E2-41 creates and lets the instance write, and whose
name reaches the collector through its environment. Prompts and responses are not stored (section
6.6): the gateway logs metadata only, and the engine's flags must keep request content out of its
own log (issue E3-04); this issue ships what the units write and adds nothing to it.

## Deliverables

- [ ] `services/engine/collector/config.yaml` scrapes the engine's metrics at port 8000 and the lifecycle agent's at port 9110, both on localhost, collects the host's CPU, memory and disk metrics, and writes everything to the workspace with signed remote write.
- [ ] It adds to every series the labels pool (from its environment), instance (the instance's ID, in place of the scrape address) and instance_type (from the instance metadata).
- [ ] It reads the journal of the instance's units and writes their lines to the pool's log group named in its environment, one stream per instance, and reads no other log.
- [ ] `services/gateway/collector/config.yaml` scrapes the gateway's metrics at 127.0.0.1 port 9091, adds the gateway task's ID as the label instance, and writes to the same workspace; both configurations read the workspace's address and the region from the environment variables RHI_WORKSPACE_URL and RHI_REGION, and the node's reads the pool and its log group from RHI_POOL and RHI_LOG_GROUP.
- [ ] `tests/unit/test_collectors.py` reads both configurations.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_collectors.py` passes: the scrape targets are localhost ports 8000 and 9110 on the node and 9091 in the gateway's task, the node's configuration has one exporter for metrics and one for logs, the gateway's one for metrics, and neither holds an address, a region, an account or a log group's name.
- [ ] `tests/unit/test_collectors.py::test_the_units_logs_reach_the_pool_log_group` shows the node's logs pipeline reading the journal of the instance's units only and writing to the log group its environment names.
- [ ] `tests/unit/test_collectors.py::test_every_series_says_where_it_comes_from` shows the node's configuration setting the labels pool, instance and instance_type on every metrics pipeline, and the gateway's setting the label instance.
- [ ] On the deployed mock pool, the lines of each instance's units appear in the pool's log group, and its series in the workspace carry the three labels (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 6.6, 7.1, 7.2
- `docs/design/evidence.md`, E3
