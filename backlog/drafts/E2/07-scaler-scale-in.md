+++
id = "E2-07"
title = "Scaler: remove an idle instance, and only an idle one"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-06"]
+++

## Context

Scale-in is where a request can be cut. The design does not lean on the load balancer's drain: the
scaler removes only an instance that reports nothing running, lifts that instance's scale-in
protection, and lowers the desired capacity (section 5.1, transition n to n minus 1, and "Scale-in
and draining").

The rule is for a pool of two instances or more: the load would fit in n minus 1 instances at half
of `target_concurrency` for 10 minutes, and 15 minutes have passed since the last scale-out. The
last instance leaves only by the rule that takes a pool to zero, after `idle_minutes` with no
request (issue E2-05).

## Deliverables

- [ ] `services/scaler/rhi_scaler/decide.py` removes one instance from a pool with n instances in service, n being 2 or more, when the requests in flight have been at most (n - 1) times half of target_concurrency for 10 minutes, and 15 minutes have passed since the last scale-out.
- [ ] The instance to release is one whose heartbeat reports nothing running and nothing waiting; when no such instance exists, nothing is removed.
- [ ] The decision names that instance, so that its scale-in protection is lifted before the desired capacity is lowered, and the time of the last scale-out and the start of the low load are kept in the memory that `services/scaler/rhi_scaler/model.py` defines.
- [ ] `tests/unit/scaler/test_scale_in.py` covers the rule.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_scale_in.py` passes: with target_concurrency 8, two instances in service, one of them reporting nothing running, and 4 requests in flight for 10 minutes, that instance is removed; with 5 in flight, or within 15 minutes of a scale-out, none is; and a single instance with no request in flight for 10 minutes is kept.
- [ ] `tests/unit/scaler/test_scale_in.py::test_a_busy_instance_is_never_released` shows that with a running request on every instance nothing is removed, whatever the load average.

## References

- `docs/design/system-design.md`, section 5.1
