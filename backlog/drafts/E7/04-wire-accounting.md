+++
id = "E7-04"
title = "Measure completion and failures on all three streaming routes"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

Use the gateway and spikes’ SSE and usage readers. Evaluation needs proof of the route’s terminal event as well as the status code.

## Deliverables

- [ ] `tools/evaluate/streams.py` records attempts, route-specific completion, error or truncation including mid-stream HTTP 200 errors, transport failure, client retries and received usage for Messages, Responses and Chat Completions.
- [ ] The same reader timestamps send, first output, first non-reasoning output, last output and completion and derives speed for eligible streams without counting pings or persisting bodies.
- [ ] `tools/evaluate/tests/test_streams.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_streams.py` replays successful and prematurely closed streams for each wire format and verifies complete accounting and fixed failure causes.
- [ ] `tools/evaluate/tests/test_streams.py` checks reasoning-only first output, ping-only chunks, tool-call deltas, single-token replies, coalesced events, missing usage and the S5 per-stream speed formula.

## References

- `docs/design/agent-session-evaluation.md`, Reading a result
- `spikes/loadgen.py`
- `spikes/forwarder.py`
