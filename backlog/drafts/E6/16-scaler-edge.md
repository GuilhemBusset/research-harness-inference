+++
id = "E6-16"
title = "Scaler: read and remember only what the reconciler needs"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-14", "E6-15"]
+++

## Context

Since E6-15 the scaler's decision reads a pool's session, its supply, its instances' heartbeats and
its launches, and nothing else (system design, section 5.1, the inputs table). The edge of the
scaler still reads and stores what the rules on load needed. This issue removes it:

- the gateways' heartbeats, which carried demand, and the pool's mode record;
- the memory of load, idleness and the gateway: `last_request_at`, `last_in_flight_at`,
  `above_target_since`, `waiting_since`, `last_scale_out_at`, `low_load_since` and
  `last_gateway_at`. The memory keeps the launch loop's mark and times, the rebalance's, the wait
  for capacity (`short_since`, E6-03) and the stretch outside sessions;
- the session switch, which the scaler read only to report. The time a pool spends in service
  outside a session, which a CloudWatch alarm reads (section 11.3), is counted against the pool's
  own session: a stretch begins when an instance is in service while the pool's session is not
  active, and no earlier than that session's end;
- the gauge `rhi_scaler_session_on`, which had no label. The dashboards read each pool's
  `rhi_scaler_session_active` since E6-14.

A memory item stored by an earlier version still holds the old attributes: the update that stores
the memory removes them, as it removes any attribute of its own that has no value. The pool-state
table may also still hold mode records and the switch's item: nothing reads them, and nothing here
deletes them.

The gateway still writes its heartbeat rows until E6-18, and reads the mode records and the switch
until then. Removing the types from `rhi_common` is E6-21's.

After this issue the deployed scaler decides and reports as after E6-15, with fewer reads on each
run. A run no longer fails because the switch could not be read.

## Deliverables

- [ ] `services/scaler/rhi_scaler/aws_io.py` reads neither the gateways' heartbeat rows nor a pool's mode record nor the session switch, and `services/scaler/rhi_scaler/model.py` holds neither a mode nor gateway heartbeats in a pool's inputs.
- [ ] The memory in `services/scaler/rhi_scaler/model.py` no longer has the seven attributes of load, idleness and the gateway that Context lists, and the update of a stored memory item removes them from an item that still holds them.
- [ ] `services/scaler/rhi_scaler/outside_sessions.py` counts each pool's stretch against that pool's own session: it begins when an instance is in service while the session is not active, no earlier than the session's end, and ends when none is in service or the session is active.
- [ ] `services/scaler/rhi_scaler/metrics.py` no longer sends the gauge rhi_scaler_session_on, and `services/scaler/rhi_scaler/handler.py` has no run that fails, or sends a gauge alone, because of the switch; `tests/unit/scaler/test_session_gauge.py` is deleted.
- [ ] `services/scaler/rhi_scaler/settings.py` gives a pool no configured mode.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler tests/synth/test_scaler.py` passes: a stored memory item with the seven old attributes is read, and the next update removes them and keeps the launch loop's mark.
- [ ] `tests/unit/scaler/test_outside_sessions.py` shows a stretch that starts at the end of the pool's session, not before it, and that a session active on another pool does not end it.
- [ ] `git grep -n -e rhi_scaler_session_on -e SessionSwitch -e GatewayHeartbeat -e CONFIGURED_MODE -- services/scaler` prints nothing.

## References

- `docs/design/system-design.md`, sections 5.1, 7.2 and 11.3
- `services/scaler/rhi_scaler/aws_io.py`
- `services/scaler/rhi_scaler/outside_sessions.py`
- `services/scaler/rhi_scaler/metrics.py`
