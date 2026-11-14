+++
id = "E2-57"
title = "A Managed Prometheus workspace, and who may write and read it"
labels = ["size:S", "kind:infra"]
epic = "E2"
blocked_by = ["E1-05", "E2-49"]
+++

## Context

Metrics from every machine, from the gateway and from the scaler go to Amazon Managed Service for
Prometheus (section 7.1): each node pushes its own, so there is nothing to discover. Managed
Prometheus is chosen over CloudWatch's PromQL store because the latter limits a query to 7 days and
500 series, which breaks month-to-date panels per student and for cost (section 7.1; evidence E19).
Per-student series exist only here, and section 6.6 keeps them until course end plus 90 days. The
workspace's retention is a period counted from when each sample is written, not a date, so it is set
to 90 days: whatever was written before course end is gone 90 days after it (issue E4-18 holds every
store to that rule with a policy test).

The workspace is a stateful resource. A Managed Prometheus workspace has no deletion protection and
no point-in-time recovery to turn on, so the protections it gets are those issue E2-88 lists for its
type: a retain policy on deletion and on replacement, and a logical ID that the record pins.

This issue adds the workspace to `tests/policy/stateful_logical_ids.json`, and changes
`tests/policy/acknowledged/`. `tests/policy/` is a protected path (ADR 0001, decision 10): the
builder may write the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/observability/prometheus.py` creates one workspace with a retention of 90 days and a retain policy on deletion and on replacement, a policy that allows remote write to it, and a policy that allows queries on it.
- [ ] `src/rhi/observability/stack.py` holds the workspace, `src/rhi/stage.py` adds the observability stack to the stage, and `tests/policy/stateful_logical_ids.json` records the workspace.
- [ ] Every cdk-nag finding `src/rhi/observability/prometheus.py` or `src/rhi/observability/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/observability_prometheus.toml` or `tests/policy/acknowledged/observability_stack.toml`.
- [ ] Every cdk-nag finding `src/rhi/stage.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/stage.toml`.
- [ ] `tests/synth/test_prometheus.py` checks the workspace and the two policies.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_prometheus.py tests/policy/test_stateful_resources.py` passes: one workspace with a retention of 90 days and a retain policy, recorded with its logical ID, and a write policy and a query policy that each name that workspace only.

## References

- `docs/design/system-design.md`, sections 6.6, 7.1
- `docs/design/evidence.md`, E19
