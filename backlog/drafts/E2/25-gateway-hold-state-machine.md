+++
id = "E2-25"
title = "Gateway: the hold, as a state machine over time"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-02", "E2-03", "E2-04"]
+++

## Context

Client retries cannot cover a GPU cold start, so the gateway holds the request (sections 3 and 5.3).
The numbers come from the clients: a stream is held for at most 420 seconds by default, a
non-streaming call for at most 240. Both are fields of the pool (issue E2-04): `hold_seconds` for a
stream and `hold_seconds_non_streaming` for a non-streaming call, with the defaults of section 17. A
held stream is answered 200 at once and kept alive every 10 seconds, with real `ping` events on
Messages, which Claude Code tolerates for about 12 minutes, and with SSE comments on the OpenAI
formats. While it holds, the gateway repeats the wake every 15 seconds (section 5.1). The keep-alive
interval is a parameter of the machine, 10 seconds by default (section 17); the configuration key
`keepalive_seconds` and the environment variable that carry it to the running gateway are issue
E2-76's. The 15 seconds are the design's own constant.

The keep-alives do not stop when the request is forwarded. They continue until the engine's first
event, because the engine is silent while it computes a long transcript, which takes tens of
seconds when its cache misses (sections 4.7 and 5.3; evidence E28), and a pool that has just started
has nothing in its cache. Once the request is forwarded the hold's deadline no longer applies; the
request's own time limit does (issue E2-77).

A hold that reaches its deadline ends with the cause `warming`, or `capacity_unavailable` when AWS
could not provide an instance. A request for a pool that will not start for this caller is not held
at all: it ends at once with `pool_disabled`, `pool_ceiling_reached` or `pool_scheduled_only`
(section 6.2, step 8). Students cannot wake a `scheduled-only` pool (section 5.1): a request for one
that a schedule or the admin has not started ends at once, and while one is starting its requests
are held without asking for a wake. The pool's mode and the scaler's note on the pool, whether it
has asked for capacity and when a launch last found none, are the records of issue E2-03.

This issue is the rule alone, over a clock the caller supplies: what to send and when, and how a
hold ends.

## Deliverables

- [ ] `services/gateway/rhi_gateway/hold.py` is a state machine with no I/O: given the pool's mode and the scaler's note of issue E2-03, the pool's readiness, the wire format, whether the request streams, and the time, it says what to do next: send a keep-alive, ask for a wake, forward the request, or end with a cause.
- [ ] A held stream gets a keep-alive at the interval it is given, 10 seconds by default, a ping event on Messages and an SSE comment on Chat Completions and Responses, from the start of its hold until the engine's first event, after the request is forwarded too; a non-streaming request is held silently.
- [ ] A wake is asked for at once and again every 15 seconds while the hold lasts, except for a scheduled-only pool, for which none is ever asked.
- [ ] The hold ends when the pool is ready, or at the deadline, the pool's hold_seconds for a stream and its hold_seconds_non_streaming for a non-streaming call, with the cause warming, or capacity_unavailable when the scaler's note says no capacity could be started.
- [ ] No request is held for a pool in mode disabled or budget-latched, nor for a scheduled-only pool with nothing in service and no capacity asked for: the request ends at once with the cause pool_disabled, pool_ceiling_reached or pool_scheduled_only respectively, and no wake is asked.
- [ ] `tests/unit/gateway/test_hold.py` covers the machine on a clock the test controls.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_hold.py` passes: with a pool of the defaults, a held Messages stream gets a ping every 10 seconds and ends at 420 seconds, a held non-streaming call ends at 240 seconds, and a pool that becomes ready at 200 seconds has its request forwarded then.
- [ ] `tests/unit/gateway/test_hold.py::test_keep_alives_continue_until_the_first_event` forwards a held stream at 200 seconds to an engine whose first event comes at 260 seconds, and finds keep-alives at 210, 220, 230, 240 and 250 seconds and none after 260, with no deadline at 420.
- [ ] `tests/unit/gateway/test_hold.py::test_wakes_repeat_every_15_seconds` counts the wakes of a full hold, and `tests/unit/gateway/test_hold.py::test_stopped_pools_are_not_held` checks the three immediate causes.
- [ ] `tests/unit/gateway/test_hold.py::test_a_scheduled_pool_is_never_woken` holds a request for a scheduled-only pool whose capacity was asked for by its schedule, without a single wake, until it is ready.

## References

- `docs/design/system-design.md`, sections 3, 4.7, 5.1, 5.3, 6.2, 17
- `docs/design/evidence.md`, E12, E28
