+++
id = "E6-17"
title = "Gateway: serve a pool while its session is active"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-09", "E6-11", "E6-13"]
+++

## Context

Students are served on a pool while that pool's session is active, and at no other time (system
design, sections 5.3 and 5.6). Today the gateway reads one switch for the whole endpoint, and serves
the owner's own entries, the canary and the spikes' students, whatever it says. Both go: a session
is per pool, and it serves every key, whoever booked it. Nothing needs to be served outside a
session any more: the verification, the canary and the spike jobs book one, or run inside one
(E6-07 to E6-13).

This issue changes what decides "no session" and nothing else of the request path. The hold, the
wake and the pool modes stay until E6-18. So:

- each gateway task reads every pool's session item (E6-01) on its 5-second cycle, in place of the
  switch, keeps the last value when a read fails, and reads no session until its first read
  succeeds;
- whether a session is active is the shared rule, `is_active`, on the task's own clock, with the
  longest session from the configuration;
- a request names its pool in its body. So the check moves: it is made once the body has named the
  pool and before a concurrency slot is taken. While no pool's session is active, the request is
  still refused before its body is read (section 6.2, step 5).

The local stack of the smoke tests serves its canary through this path, so its fixture books a
session on the pool under test.

After this issue the deployed gateway answers `no_session` for a pool that nobody has booked,
to any key. The switch is no longer read by the gateway; the admin CLI's `session on` and `off`
then change nothing, until E6-20 removes them.

## Deliverables

- [ ] `services/gateway/rhi_gateway/readiness.py` reads each pool's session item on every cycle and no longer reads the session switch; a failed read keeps the session last read, and a pool has no session until a first read succeeds; `tests/unit/gateway/test_session_switch.py`, which held the switch's behaviour, is deleted.
- [ ] `services/gateway/rhi_gateway/app.py` refuses a forwarded request with the cause `no_session` when the session of the pool its body names is not active, before a slot is taken, for every key; while no pool's session is active it refuses before the body is read.
- [ ] `services/gateway/rhi_gateway/local.py` lists a pool as unavailable with `no_session` exactly when that pool's own session is not active, and answers a wake for such a pool with `no_session`, asking for nothing.
- [ ] `services/gateway/rhi_gateway/settings.py` reads the longest session from the task's environment, and `src/rhi/gateway/service.py` gives it the configuration's `session_max_hours`.
- [ ] `tests/smoke/conftest.py` books a session on the pool under test in the local stack's table, so that the smoke tests pass against the local stack.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_sessions.py` passes: with a session active on one pool and none on another, a request for the first is served and one for the second is answered `no_session`, for a student's key and for the canary's; with no session at all, a request is refused before its body is read.
- [ ] `uv run --locked pytest tests/unit/gateway tests/smoke tests/replay tests/synth/test_gateway_service.py` passes: the task's environment gives the longest session, and the smoke tests pass against the local stack in the session its fixture booked.
- [ ] In the pipeline, `tests/smoke/test_smoke.py` passes on the mock pool in the session the verification booked (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.3, 5.6 and 6.2
- `services/gateway/rhi_gateway/readiness.py`
- `services/gateway/rhi_gateway/app.py`
- `services/common/rhi_common/sessions.py`, delivered by E6-01
