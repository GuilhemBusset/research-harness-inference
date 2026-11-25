+++
id = "E6-27"
title = "Gateway: its metrics, its log and its role without the hold"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-18"]
+++

## Context

Since E6-18 the gateway holds no request, asks the scaler for nothing and writes no heartbeat row.
Four things still carry the hold's trace, and go here:

- **The metrics.** The gateway's histograms of what students experience, the time to the first
  token, the time to the first answer and the output speed of a stream, carry a label that tells a
  held request from a warm one, and a gauge counts the requests held (system design, section 7.2).
  Every request now reads warm and the gauge reads zero. The label and the gauge go, and the
  dashboards that select the warm requests by that label read the histograms without it.
- **The demand heartbeat's module.** It no longer writes rows; what is left of it is this task's
  count of requests in flight, which the gauge of requests in flight reads. The count moves beside
  the gauge, and the module goes, with the last use of the shared heartbeat record in the gateway.
- **The log record** of a request carries the time it was held (section 6.2). It goes.
- **The task role and its environment.** The role may invoke the scaler's function and write the
  heartbeats table, and the task is given the function's name, the table's name and the
  keep-alive interval of a held stream. The gateway uses none of them. The role keeps its read and
  write of the usage table, which the concurrency slot and the affinity cookie use.

A series that loses a label is a new series to Prometheus: the objectives read, over a range that
spans this deployment, only the requests after it. That is accepted: no class has run.

`tests/policy/` is a protected path (ADR 0001, decision 10). This issue names the one
acknowledgement file it changes there, which gives reasons for the two grants the role loses.

After this issue the deployed gateway's role is narrower, and its metrics have one label fewer.

## Deliverables

- [ ] `services/gateway/rhi_gateway/experience.py` gives its three histograms no label for a held request, has no gauge of requests held, and feeds the gauge of requests in flight from a count it keeps itself; `services/gateway/rhi_gateway/heartbeat.py` and `tests/unit/gateway/test_heartbeat.py` are deleted.
- [ ] `services/gateway/rhi_gateway/telemetry.py` writes no time held in a request's log record.
- [ ] `dashboards/rhi_dashboards/objectives.py` and `dashboards/rhi_dashboards/inference.py` read the histograms without that label, and no panel reads a gauge of requests held.
- [ ] `src/rhi/gateway/service.py` gives the task role neither the invocation of the scaler's function nor a write of the heartbeats table, and gives the task neither the function's name, nor that table's name, nor a keep-alive interval; `services/gateway/rhi_gateway/settings.py` and `services/gateway/rhi_gateway/main.py` read none of the three.
- [ ] `tests/policy/acknowledged/gateway_service.toml` acknowledges nothing about the scaler's function or the heartbeats table.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway tests/unit/dashboards` passes: no series of the gateway carries the label, the gauge of requests in flight counts a forwarded request from its forwarding to its end, and every objective is built without the label.
- [ ] `uv run --locked pytest tests/synth/test_gateway_service.py tests/policy` passes: the task role holds no action of Lambda and no action on the heartbeats table.
- [ ] `git grep -n -e RHI_SCALER_FUNCTION -e RHI_HEARTBEAT_TABLE -e RHI_KEEPALIVE_SECONDS -e rhi_gateway_requests_held -e GatewayHeartbeat -- services/gateway src dashboards` prints nothing.

## References

- `docs/design/system-design.md`, sections 6.2 and 7.2
- `services/gateway/rhi_gateway/experience.py`
- `services/gateway/rhi_gateway/heartbeat.py`
- `src/rhi/gateway/service.py`
- `dashboards/rhi_dashboards/objectives.py`
