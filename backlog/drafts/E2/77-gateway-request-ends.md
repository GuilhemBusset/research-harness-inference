+++
id = "E2-77"
title = "Gateway: every end of a forwarded request settles once, with the causes engine_error and time_limit"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-24"]
+++

## Context

A request that does not end well must still be charged, and still be told why in its own wire format
(section 6.2, steps 3, 7 and 8). Any end other than a clean one (a client that disconnects, an
engine that fails, a timeout) charges the tokens observed and never less than the input estimate.
Issue E2-24 forwards and streams a request; this issue handles every way it can end.

Two causes of section 6.2, step 8, belong to this path. `engine_error`, a 502, is for an engine that
failed or ended a response early; on a stream that has started it is that format's error event.
`time_limit`, a 504, is for a request that reached the limit; on a stream it is that format's error
event. A request may run 30 minutes at most, a default of section 17. The limit is a parameter of
the proxy, 1,800 seconds by default; the configuration key `longest_request_seconds` and the
environment variable that carry it to the running gateway are issue E2-76's.

Students paste errors into public issues, so nothing of the engine's own text reaches them (section
5.3); the rendering is issue E2-02's.

## Deliverables

- [ ] `services/gateway/rhi_gateway/proxy.py` settles every end of a forwarded request exactly once: a clean end with the reported usage; a client disconnect, an upstream error or a timeout with the tokens observed, never less than the input estimate.
- [ ] An engine that fails, or ends a response early, reaches the client with the cause engine_error of issue E2-02: a 502 before anything was sent, and the error event of the request's wire format on a stream that has started; never the engine's own text.
- [ ] A request is ended at the longest duration it is given, 1,800 seconds by default, with the cause time_limit: a 504 for a non-streaming call, and the error event of its wire format on a stream.
- [ ] `tests/unit/gateway/test_request_ends.py` covers every end against an upstream built in the test, on a clock the test controls.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_request_ends.py` passes, and `tests/unit/gateway/test_request_ends.py::test_every_end_settles_once` covers a clean end, a client that disconnects mid-stream, an upstream that fails mid-stream and a request that reaches 1,800 seconds, each settled once.
- [ ] `tests/unit/gateway/test_request_ends.py::test_engine_error` shows a 502 with engine_error for an upstream that answers 500, and the error event with engine_error for one that stops mid-stream, with nothing of the upstream's text in either.
- [ ] `tests/unit/gateway/test_request_ends.py::test_time_limit` shows a 504 with time_limit for a non-streaming call at 1,800 seconds, and the error event with time_limit for a stream at 1,800 seconds, in each wire format.

## References

- `docs/design/system-design.md`, sections 5.3, 6.2, 17
