+++
id = "E6-18"
title = "Gateway: no hold and no wake"
labels = ["size:L", "kind:app"]
epic = "E6"
blocked_by = ["E6-15", "E6-17"]
+++

## Context

A request never starts a machine, so the gateway holds none (system design, section 5.3). It
forwards a request when the pool has an instance in service, and answers at once when it has not.
A hold covers a start of a few minutes; what a pool without an instance waits for is the start of
its session or AWS's capacity, and no client's patience covers either.

What a request gets from a pool that cannot serve it, each as a 503 in the request's wire format:

| Cause | When | `Retry-After` |
| --- | --- | --- |
| `no_session` | The pool's session is not active (E6-17) | none |
| `warming` | The session is active, no target is healthy, and the scaler's note counts at least one instance starting | as configured, 30 s by default |
| `capacity_unavailable` | The session is active, no target is healthy and none is starting | as configured, 60 s by default |

The note's count of starting instances is E6-03's. A note without it reads as none starting.

`GET /status` gives each pool one of three states, with a cause when it is unavailable, and the
time its session ends while that session is active:

| State | Cause | When |
| --- | --- | --- |
| `ready` | | The session is active and the pool has a healthy target |
| `warming` | | The session is active, no target is healthy and at least one instance is starting |
| `unavailable` | `capacity_unavailable` | The session is active, and no instance is in service or starting |
| `unavailable` | `no_session` | The pool's session is not active |

What goes: the hold's state machine and its keep-alives; the answer of 200 before the pool is
ready, and the header that marked it; the wake route, the calls that ask the scaler to run and the
explicit wakes; the rows of the demand heartbeat, which the scaler no longer reads (E6-15); the
state `cold`; and the pool modes with their two causes, `pool_scheduled_only` and `pool_disabled`,
which the gateway no longer answers. The shared list of causes keeps the two names until E6-19,
which removes them where the student guide and a spike still name them.

The smoke tests assert today that a first stream wakes a cold pool and is held. That test goes, and
one comes: a request to a pool nobody has booked is answered `no_session`. On a deployed stack the
verification books the pool before the stream tests, so it runs that one test first (section 8.2,
step 6). `tests/smoke/ids.py` is the reporter's list of test IDs, so the IDs change with the tests.
One examiner's probe, `tests/examiner/test_issue69_hold_probe.py`, holds the hold's contract, which
no longer exists: it is deleted with the hold.

The concurrency slot is a lease, taken for the pool's hold, the longest request and a margin
(`rhi_gateway.budget`). With no hold it is taken for the longest request and the margin.

The module of the demand heartbeat also keeps this task's count of requests in flight, which a
gauge of the gateway reads. That count stays; the module only stops writing rows. Left to E6-27:
that module and its gauge, the label that told a held request from a warm one in the gateway's
histograms, the time held in the log record, and the task role's grants. After this issue no
request is held, so the label reads warm on every request, and the grants are unused. The student
guide and `rhi wait` follow in E6-19.

Why the size: the request path, the local routes and the smoke tests change together, and four test
modules go with the hold.

After this issue the deployed gateway holds nothing: every request is forwarded or answered at
once.

## Deliverables

- [ ] `services/gateway/rhi_gateway/app.py` answers a request for a pool whose session is active and that has no healthy target at once, before a slot is taken, with `warming` when the scaler's note counts an instance starting and `capacity_unavailable` when it counts none; a token count gets the answer a request would get.
- [ ] `services/gateway/rhi_gateway/hold.py` and `services/gateway/rhi_gateway/waker.py` are deleted, with the held path of `services/gateway/rhi_gateway/proxy.py` and what `services/gateway/rhi_gateway/main.py` built for them: no keep-alive, no answer of 200 before the pool is ready, no header that marks a held request, and no call to the scaler.
- [ ] `services/gateway/rhi_gateway/heartbeat.py` writes no row to the heartbeats table and keeps only the task's count of requests in flight, and `services/gateway/rhi_gateway/budget.py` takes a slot's lease for the longest request and its margin, with no hold added.
- [ ] `services/gateway/rhi_gateway/routes.py` has no route for a wake, which the gateway answers 404 with `not_found` like any path outside its table, and `src/rhi/gateway/service.py` no longer forwards the wake's path from the public load balancer.
- [ ] `services/gateway/rhi_gateway/local.py` gives the states and causes of the status table in Context, with the session's end for a pool whose session is active, and `services/gateway/rhi_gateway/readiness.py` reads no pool's mode record: the gateway never answers `pool_scheduled_only` or `pool_disabled`.
- [ ] `tests/smoke/test_smoke.py` has no test of a wake or a hold and one test, with the ID no-session in `tests/smoke/ids.py`, that a request to a pool nobody has booked is answered 503 with `no_session`; `tests/smoke/conftest.py` makes the local stack's pool ready a moment after its session is booked; and `scripts/pipeline/verify.py` runs that test once, on the mock pool before it books it, and in no other run.
- [ ] `tests/unit/gateway/test_hold.py`, `tests/unit/gateway/test_hold_path.py`, `tests/unit/gateway/test_wake.py` and `tests/examiner/test_issue69_hold_probe.py` are deleted.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_not_ready.py` passes: a stream and a call that is not one, on each wire format, get a 503 with `warming` and its `Retry-After` when an instance is starting and with `capacity_unavailable` when none is, with no slot taken; and a token count gets the same.
- [ ] `tests/unit/gateway/test_local.py::test_the_status_table` shows each of the four rows of the status, the session's end on a pool whose session is active, and no state `cold`.
- [ ] `uv run --locked pytest tests/unit/gateway tests/unit/common tests/smoke tests/unit/test_verify_script.py tests/unit/test_docs.py tests/unit/reporter tests/synth/test_gateway_routing.py` passes: the wake's path is not found, at the gateway and at the public load balancer's rules.
- [ ] `git grep -n -e x-rhi-held -e rhi_gateway.hold -e rhi_gateway.waker -e hold_seconds -- services/gateway/rhi_gateway/app.py services/gateway/rhi_gateway/budget.py services/gateway/rhi_gateway/local.py services/gateway/rhi_gateway/main.py services/gateway/rhi_gateway/proxy.py src scripts tests/smoke` prints nothing.
- [ ] In the pipeline, `tests/smoke/test_smoke.py` passes: the request to the unbooked mock pool is answered `no_session`, and the streams complete in the session the step booked (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.3, 6.2 and 8.2
- `services/gateway/rhi_gateway/app.py`
- `services/gateway/rhi_gateway/hold.py`
- `services/gateway/rhi_gateway/local.py`
- `tests/smoke/test_smoke.py`
- `tests/examiner/test_issue69_hold_probe.py`
