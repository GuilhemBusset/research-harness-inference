+++
id = "E2-31"
title = "A mock engine that serves the engine contract's routes with canned streams"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16"]
+++

## Context

A model server is anything that satisfies the engine contract of section 4.1. The mock is a CPU
container that satisfies it with canned streams, so that the whole control loop (hold, wake,
lifecycle, drain, verification) is testable for cents, in CI and in the deployed stack. The mock
pool runs it on `t4g.small`, an arm64 instance (section 4.2). It also imitates the reason the route
allow-list exists: like vLLM, it answers some routes without its secret (evidence E23). An engine
does not know its image digest: the identity row of the contract belongs to the instance's
lifecycle agent (issue E2-35), not to the mock.

The coursework model reasons before it answers (section 4.2), and the gateway measures the time to
the first token that is not reasoning (section 7.2), so the mock's answers begin with reasoning.

This issue is the mock's routes and the settings that shape its answers. Its metrics, its limit on
running requests, its shutdown and its image are issue E2-82; its imitation of a prefix cache is
issue E2-75. Like every engine, it listens on port 8000 (section 4.1). It reads the pool's bearer secret at start
from the environment variable RHI_POOL_SECRET, the name under which the engine's unit hands the
secret to the container (issue E2-70), and does not start without it, so that the four routes are
never served unguarded. The web framework and the
server are those issue E1-16 pins.

## Deliverables

- [ ] `services/engine/mock/rhi_mock_engine/app.py` serves the four inference routes of the contract with canned answers, streamed and not, in each wire format's own sequence of events: reasoning, then an answer or a tool call, then usage whose input is a token count the mock derives deterministically from the request.
- [ ] The four routes require the pool's bearer secret, which the mock reads at start from the environment variable RHI_POOL_SECRET and without which it does not start, and `GET /health` answers 200 only once a configurable start delay has passed.
- [ ] Like vLLM, it answers the routes /invocations, /pause and /tokenize without the secret.
- [ ] Its settings, for tests and for the mock pool, are the delay before the first byte, the rate of the stream, the start delay, and a mode in which inference hangs while the health route keeps answering; it listens on port 8000.
- [ ] `tests/unit/engine/test_mock.py` covers the application in process.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/engine/test_mock.py` passes: each inference route answers in its format, streamed and not, with reasoning before the answer, and refuses a request without the secret given in RHI_POOL_SECRET; the mock refuses to start with RHI_POOL_SECRET unset or empty; the health route answers 503 before the start delay and 200 after.
- [ ] `tests/unit/engine/test_mock.py::test_unguarded_routes_like_vllm` shows `/invocations`, `/pause` and `/tokenize` answered without the secret.
- [ ] `tests/unit/engine/test_mock.py::test_the_hang_mode` shows an inference request that never answers while the health route answers 200.

## References

- `docs/design/system-design.md`, sections 4.1, 4.2, 7.2
- `docs/design/evidence.md`, E23
