+++
id = "E2-21"
title = "Gateway: read token usage from a Messages response"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-20"]
+++

## Context

The three wire formats report usage differently (section 6.2, step 6). Messages reports it across
two events: input tokens in `message_start`, output tokens in `message_delta`. The parser must know,
at any moment, what has been observed so far, because a stream that ends badly is charged the tokens
observed.

The daily budget counts every input token, cached or not (sections 11.3 and 17). In Anthropic's
reference the usage object reports the input in separate fields, uncached input, input written to
the cache and input read from it, which add up to the whole input. The parser counts all three, so
cached input is charged. A field a response does not carry counts as zero.

## Deliverables

- [ ] `services/gateway/rhi_gateway/usage/messages.py` reads the input tokens from the message_start event and the output tokens from the last message_delta event of a stream, and the usage object of a non-streaming answer.
- [ ] Its input is the sum of the usage object's input fields as Anthropic's reference defines them, the cached ones included, taking for each field the last value the stream reported.
- [ ] At any point it reports the tokens observed so far, and whether the stream ended cleanly.
- [ ] `tests/unit/gateway/test_usage_messages.py` covers a stream and a non-streaming answer.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_usage_messages.py` passes: the totals of a recorded stream are found whatever the byte boundaries, and a stream cut after each event reports what was observed up to that event.
- [ ] `tests/unit/gateway/test_usage_messages.py::test_cached_input_is_counted` reads a stream whose usage reports 2,000 uncached input tokens and 80,000 read from the cache, and reports 82,000 input tokens.

## References

- `docs/design/system-design.md`, sections 6.2, 11.3, 17
