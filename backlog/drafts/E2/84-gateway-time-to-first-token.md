+++
id = "E2-84"
title = "Gateway: time to the first token and to the first answer, warm or held, first turn or later"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-74"]
+++

## Context

What a student waits for is the first token, and then the first token that is not reasoning, which
is when they see an answer begin: the model reasons before it answers (section 4.2). The gateway
measures both, per pool and wire format and never labelled by student (section 7.2):

- a histogram from request accepted to first token sent, labelled warm or held, and first turn or
  later; the objective of section 7.3 counts the first turns of sessions apart;
- a second one, with the same labels, to the first token that is not reasoning.

A request is held when it waited for its pool, and warm otherwise. A first turn is a request whose
conversation carries no earlier output of the model. Which events carry reasoning and which carry
the answer or a tool call is given by each format's reference; on Chat Completions, whose reference
has no reasoning field, a delta that carries neither answer text nor a tool call is reasoning. Both
times only mean something on a stream, where tokens are sent as they come.

The objective of section 7.3 is a time to first token under 5 seconds at the 95th percentile
(section 17). A floor that falls between two buckets cannot be read exactly, so 5 seconds is a
bucket boundary of both histograms. The module and its metrics are issue E2-74's.

## Deliverables

- [ ] `services/gateway/rhi_gateway/experience.py` observes, for each streamed request, per pool and wire format, the seconds from the request accepted to the first token sent, labelled warm or held and first turn or later.
- [ ] With the same labels it observes the seconds from the request accepted to the first answer text or tool call sent, after any reasoning, as each format's reference marks them.
- [ ] Both histograms have a bucket boundary at 5 seconds, and neither carries a student label.
- [ ] `services/gateway/rhi_gateway/proxy.py` and `services/gateway/rhi_gateway/app.py` report each streamed request's first token and first answer times, whether it was held and whether it is a first turn, and `tests/unit/gateway/test_time_to_first_token.py` covers the two histograms.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_time_to_first_token.py` passes: a held Messages stream whose first reasoning event is sent at 12 seconds and whose first text event at 20 seconds, on a clock the test controls, adds 12 to the held histogram of the first token and 20 to that of the first answer.
- [ ] `tests/unit/gateway/test_time_to_first_token.py::test_first_turn_or_later` labels a request with one user message a first turn and one that carries an earlier answer of the model a later turn, in each wire format.
- [ ] `tests/unit/gateway/test_time_to_first_token.py::test_chat_reasoning_deltas` counts a Chat Completions delta with neither answer text nor a tool call as reasoning, and the first delta with answer text as the first answer.

## References

- `docs/design/system-design.md`, sections 4.2, 7.2, 7.3, 17
