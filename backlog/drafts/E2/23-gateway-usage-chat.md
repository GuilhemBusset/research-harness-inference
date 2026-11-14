+++
id = "E2-23"
title = "Gateway: read token usage from a Chat Completions answer, and hide what the client did not ask for"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-20"]
+++

## Context

Chat Completions reports usage in an optional final chunk. The gateway asks the engine for it on
every stream (issue E2-18) and hides it from clients that did not ask for it (section 6.2, step 6).
Hiding it means changing the stream that reaches the client, the one place where the gateway does
not pass bytes through untouched.

The daily budget counts every input token, cached or not (sections 11.3 and 17). In OpenAI's
reference the prompt tokens of the usage object already include the cached ones, which it details
apart; the parser takes the input as reported and does not add the cached part again.

## Deliverables

- [ ] `services/gateway/rhi_gateway/usage/chat.py` reads the usage from the final chunk of a stream and from the usage object of a non-streaming answer, with the input tokens as reported, cached ones included and not counted twice.
- [ ] When the client did not ask for usage, the final usage chunk is removed from what the client receives and every other byte is delivered unchanged; when the client asked, nothing is removed.
- [ ] At any point it reports the tokens observed so far and whether the stream ended cleanly.
- [ ] `tests/unit/gateway/test_usage_chat.py` covers both kinds of client.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_usage_chat.py` passes: the totals of a recorded stream are found whatever the byte boundaries.
- [ ] `tests/unit/gateway/test_usage_chat.py::test_cached_input_is_counted_once` reads a usage of 82,000 prompt tokens of which 80,000 are detailed as cached, and reports 82,000 input tokens.
- [ ] `tests/unit/gateway/test_usage_chat.py::test_the_usage_chunk_is_hidden` shows that a client that did not ask receives the recorded stream minus exactly the usage chunk, and one that asked receives every byte.

## References

- `docs/design/system-design.md`, sections 6.2, 11.3, 17
