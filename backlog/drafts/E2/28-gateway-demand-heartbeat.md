+++
id = "E2-28"
title = "Gateway: report demand to the scaler every 5 seconds"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-03", "E2-26", "E2-27"]
+++

## Context

The scaler reads demand from a heartbeat row that each gateway task rewrites every 5 seconds; rows
older than 20 seconds are ignored (section 5.1; the numbers are defaults of section 17). Summing
gauges across tasks gives wrong values, which is why each task writes its own row.

Demand is requests in flight and requests held, per pool, and the time of the last request
received, which the rule that takes a pool to zero reads (section 5.1; issue E2-03). Status calls
and token counting are not demand. An explicit wake (`POST /wake`, issue E2-78) is: it counts as a
held request for 10 minutes or until the pool is ready, whichever comes first, and as a request
received at the moment the pool is ready. The waker of issue E2-73 keeps the wakes that are pending.

The heartbeat is given its table and the task's ID; issue E2-76 reads both when the gateway starts
(the table from the gateway's environment, the task's ID from the task metadata ECS gives the
container).

## Deliverables

- [ ] `services/gateway/rhi_gateway/heartbeat.py` counts, per pool, this task's requests in flight and requests held, keeps the time the last request for the pool was received, and rewrites its row with them every 5 seconds, in the form issue E2-03 defines, to the table and under the task's ID it is given.
- [ ] An explicit wake that is pending in the waker of issue E2-73 counts as one request held for its pool; at the moment its pool is ready it stops counting and is recorded as a request received at that moment, and one that ends after 10 minutes without its pool ready stops counting and records nothing.
- [ ] Status calls, token counting, the models route and the hello route change neither count nor the time of the last request.
- [ ] `services/gateway/rhi_gateway/app.py` starts the heartbeat with the application, counts a request as held while it is held and as in flight from its forwarding to its end, and on shutdown the task writes a row of zeros.
- [ ] `tests/unit/gateway/test_heartbeat.py` covers the counts and the writes.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_heartbeat.py` passes: two requests in flight and one held give a row of 2 and 1, rewritten every 5 seconds on a clock the test controls, with the time of the latest of the three as the last request received.
- [ ] `tests/unit/gateway/test_heartbeat.py::test_cheap_calls_are_not_demand` shows status calls and token counting leave the counts at zero and the last request time unchanged.
- [ ] `tests/unit/gateway/test_heartbeat.py::test_an_explicit_wake_is_demand` shows a pending wake as one request held, and, once its pool is ready, a held count of zero and a last request received at that moment.

## References

- `docs/design/system-design.md`, sections 5.1, 5.3, 17
