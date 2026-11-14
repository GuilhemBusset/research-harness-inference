+++
id = "E2-11"
title = "Scaler: one run from inputs to calls, with its settings and its metrics"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-08", "E2-10"]
+++

## Context

The scaler is a function that runs one invocation at a time, called by the gateway when a request
needs a cold pool, by a one-minute schedule and by the admin CLI (section 5.1). Each run reads the
same inputs and decides for every pool. What it decided, and why, is what the fleet dashboard shows
(section 7.2).

The function's construct (issue E2-12) gives it its settings in one environment variable,
`RHI_SCALER_SETTINGS`, a JSON object that this issue's reader defines: each pool's entry as the
configuration gives it, parsed again with `services/common/rhi_common/pool_config.py`, with its
group's name and its target group's ARN; the names of the heartbeat table and of the pool-state
table; and the workspace's remote-write address. The region is the one Lambda sets in `AWS_REGION`.
Later issues add to this object (the time zone, the session windows and the pre-warm leads, issues
E4-01 and E4-05; the class-day topic's ARN, issue E4-06, where no ARN means no notice), each with a
default, so that an older construct's settings still read.

The metrics go to Managed Prometheus by signed remote write (section 7.1): a protobuf message,
compressed with snappy's block format by cramjam and signed with SigV4 by the AWS SDK, the scaler
group's libraries of issue E1-16. The two message types of remote write are declared from a
descriptor at import, so nothing runs protoc and no generated code is checked in.

## Deliverables

- [ ] `services/scaler/rhi_scaler/settings.py` reads the settings of Context from the environment variable RHI_SCALER_SETTINGS and refuses, naming the field, a missing table name, a missing workspace address, or a pool whose entry does not parse.
- [ ] `services/scaler/rhi_scaler/handler.py` runs one cycle: read the settings and the inputs, decide, apply, store the memory; the caller's identity changes nothing in the decision.
- [ ] `services/scaler/rhi_scaler/metrics.py` sends to the Prometheus workspace, by signed remote write, per pool: three gauges (the desired capacity, the instances in service and the warm instances) and one counter of decisions labelled by their reason. No metric carries a student's identity.
- [ ] Each run logs one record per pool with the decision and its reason, and nothing from a request.
- [ ] `tests/unit/scaler/test_handler.py` covers a cycle against the stubbed edge, and `tests/unit/scaler/test_settings.py` covers the reader.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_handler.py` passes: a cycle with one held request sets the desired capacity to 1, stores the memory, and emits for that pool the three gauges and the decision counter with its reason.
- [ ] `tests/unit/scaler/test_handler.py::test_a_second_run_is_harmless` shows that the same inputs twice make the same decision and no second Auto Scaling call.
- [ ] `uv run --locked pytest tests/unit/scaler/test_settings.py` passes: the settings of a configuration with the mock pool read back with the pool's entry, group, target group, both tables and the workspace, and a settings object without the pool-state table is refused naming it.

## References

- `docs/design/system-design.md`, sections 5.1, 7.1, 7.2
