+++
id = "E2-06"
title = "Scaler: add an instance when one is not enough"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-05"]
+++

## Context

A cold start is not overload: the first instance is one instance. A pool grows only when the
instances in service are measurably full (section 5.1, transition 1 to n): the requests in flight,
averaged over the instances in service, above `target_concurrency` for 60 seconds; or an instance
reporting waiting requests for 60 seconds while no other instance of the pool has room. One
instance at a time, up to `max_instances`, and instances still starting count as supply.

Requests in flight are the gateway's (the sum over its fresh heartbeats for the pool); running and
waiting are each instance's own (its lifecycle agent's heartbeat). `target_concurrency` is also the
engine's limit on requests running at once (section 17), so an instance has room when it reports
fewer running requests than `target_concurrency` and nothing waiting.

An instance that waits while another of the pool has room is not a reason to grow: it is the
rebalance transition of phase 2, which moves a few students to the emptier instance and changes no
size (sections 5.1 and 5.5). This issue adds nothing for that case.

## Deliverables

- [ ] `services/scaler/rhi_scaler/decide.py` adds one instance when the requests in flight, summed over the pool's fresh gateway heartbeats and divided by the instances in service, have been above target_concurrency for 60 seconds; with no instance in service this rule adds nothing.
- [ ] It adds one instance when an instance has reported waiting requests for 60 seconds while no other instance of the pool has room, an instance with room being one that reports fewer running requests than target_concurrency and nothing waiting.
- [ ] It adds one instance at a time, never above max_instances, and adds none while an instance it asked for is still starting.
- [ ] The 60 seconds are kept in the memory that `services/scaler/rhi_scaler/model.py` defines, which the function receives and returns; the function still reads no clock.
- [ ] `tests/unit/scaler/test_scale_out.py` covers both conditions.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_scale_out.py` passes: with target_concurrency 8 and two instances in service, 17 requests in flight for 60 seconds add one, 17 for 59 seconds add nothing, and 16 for 10 minutes add nothing.
- [ ] `tests/unit/scaler/test_scale_out.py::test_waiting_with_no_room_elsewhere` shows a single instance in service that reports waiting requests for 60 seconds adding one, and the same instance beside one that has room adding nothing.
- [ ] `tests/unit/scaler/test_scale_out.py::test_starting_instances_count` shows no second addition while the first added instance is still starting, and none above max_instances.

## References

- `docs/design/system-design.md`, sections 4.7, 5.1, 5.5, 17
