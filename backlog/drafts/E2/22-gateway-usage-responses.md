+++
id = "E2-22"
title = "Gateway: read token usage from a Responses answer"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-20"]
+++

## Context

The Responses format reports usage in its terminal event (section 6.2, step 6). Until that event
arrives nothing is known, so a stream cut earlier reports no observed tokens and is charged the
input estimate.

The daily budget counts every input token, cached or not (sections 11.3 and 17). In OpenAI's
reference the input tokens of the usage object already include the cached ones, which it details
apart; the parser takes the input as reported and does not add the cached part again.

## Deliverables

- [ ] `services/gateway/rhi_gateway/usage/responses.py` reads the usage from the terminal event of a stream and from the usage object of a non-streaming answer, with the input tokens as reported, cached ones included and not counted twice.
- [ ] At any point it reports the tokens observed so far, which is nothing before the terminal event, and whether the stream ended cleanly.
- [ ] `tests/unit/gateway/test_usage_responses.py` covers a stream and a non-streaming answer.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_usage_responses.py` passes: the totals of a recorded stream are found whatever the byte boundaries, and a stream cut before its terminal event reports nothing observed and no clean end.
- [ ] `tests/unit/gateway/test_usage_responses.py::test_cached_input_is_counted_once` reads a usage of 82,000 input tokens of which 80,000 are detailed as cached, and reports 82,000 input tokens.

## References

- `docs/design/system-design.md`, sections 6.2, 11.3, 17
