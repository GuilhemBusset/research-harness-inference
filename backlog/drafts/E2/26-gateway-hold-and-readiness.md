+++
id = "E2-26"
title = "Gateway: hold a real request until the pool is ready"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-25", "E2-73", "E2-77"]
+++

## Context

The hold's rule is issue E2-25, and what each gateway task knows of each pool, read every 5 seconds,
is issue E2-73. This issue puts the hold in the request path (section 6.2, step 5): after
authentication, reservation and validation, the gateway checks from its cached values whether the
pool can serve; if not, it wakes the scaler, holds, and forwards the held request on the same
connection when the pool is ready.

A held stream is answered 200 at once with keep-alives, and the real stream follows on the same
connection. The keep-alives continue after the request is forwarded, until the engine's first
event: a pool that has just started has nothing in its cache, and the engine is silent while it
computes the transcript (section 5.3). So the proxy of issues E2-24 and E2-77 keeps sending them
after it forwards. Because the client already has its 200, anything that goes wrong after that, an
engine that refuses the request included, ends the stream with that format's error event (section
6.2, step 8).

Token counting is a cheap call: it never wakes a GPU and is not demand. On a pool that is not ready
it answers `warming` at once, and Claude Code then falls back to its own estimate (section 5.3;
evidence E15).

## Deliverables

- [ ] `services/gateway/rhi_gateway/app.py` runs the hold between the body filter and the proxy, from the kept values of issue E2-73 and the rule of issue E2-25, and answers a held stream 200 at once with the keep-alives of its format, the real stream following on the same connection.
- [ ] `services/gateway/rhi_gateway/proxy.py` keeps sending the held stream's keep-alives after it forwards the request, until the engine's first event, and ends a held stream whose engine answers with an error status, or fails before its first event, with the error event of its wire format and the cause engine_error, never with the engine's text.
- [ ] A hold that ends at its deadline settles the reservation with nothing observed and ends the request with its cause in the fixed form of issue E2-02.
- [ ] A token-counting request for a pool that is not ready ends at once with the cause warming, asks for no wake, and is not held.
- [ ] `tests/unit/gateway/test_hold_path.py` covers the path against a target group that becomes healthy after a set time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_hold_path.py` passes: a stream held 30 seconds on a controlled clock, then forwarded to an engine that stays silent for 40 seconds, receives keep-alives through those 70 seconds, none after the engine's first event, and the real stream on the same connection, in each wire format.
- [ ] `tests/unit/gateway/test_hold_path.py::test_the_deadline_ends_cleanly` shows a stream ended by the error event of its format and a non-streaming call answered 503 with Retry-After, both with the cause warming and the reservation settled.
- [ ] `tests/unit/gateway/test_hold_path.py::test_an_engine_refusal_after_the_hold` shows a held stream whose engine answers 400 ended by the error event of its format with the cause engine_error and nothing of the engine's text.
- [ ] `tests/unit/gateway/test_hold_path.py::test_a_failed_read_keeps_the_hold` shows that a read of the health that fails neither ends the hold nor forwards the request.
- [ ] `tests/unit/gateway/test_hold_path.py::test_token_counting_never_waits` shows a token-counting request on a cold pool answered 503 with the cause warming at once, with no wake asked and no reservation left open.

## References

- `docs/design/system-design.md`, sections 5.1, 5.3, 6.2
- `docs/design/evidence.md`, E15
