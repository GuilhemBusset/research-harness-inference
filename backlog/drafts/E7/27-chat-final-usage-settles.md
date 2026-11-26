+++
id = "E7-27"
title = "Settle a Chat Completions allowance from the final usage chunk"
labels = ["size:S", "kind:app"]
epic = 744
+++

## Context

`tools/evaluate/streams.py` decides whether an attempt's output count is the answer's own (`Attempt.output_counted`), and `tools/evaluate/forwarding.py` settles a request's allowance from it (`_charge`). On the Chat Completions route a stream whose last chunks are a usage chunk with no choices and then `[DONE]`, and that never sent a `finish_reason`, is read as having only an opening count: `output_counted` is false and the request is charged its whole allowance. With 10 tokens left and one token generated, the next request is refused for the token limit. A count that arrives before the answer stopped, as the one that opens a Messages stream does, must still keep the conservative charge.

## Deliverables

- [ ] `tools/evaluate/streams.py` reads the usage chunk that ends a Chat Completions stream, a chunk with no choices after the answer's output and before `[DONE]`, as the answer's own count.
- [ ] `tools/evaluate/tests/test_streams.py` and `tools/evaluate/tests/test_forwarding.py` hold a Chat Completions stream with no `finish_reason` event, a final usage chunk and `[DONE]`.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_streams.py` shows the attempt of that stream is completed with `output_counted` true and the output tokens the usage chunk reports.
- [ ] `tools/evaluate/tests/test_forwarding.py` shows that with 10 tokens left and one token reported, the request settles at 1 token and the next request is admitted with 9 available.
- [ ] `tools/evaluate/tests/test_streams.py` shows a count that comes before the answer's output still leaves `output_counted` false on every route.
- [ ] `uv run --locked pytest tools/evaluate/tests/test_streams.py tools/evaluate/tests/test_forwarding.py` exits 0.

## References

- `docs/design/agent-session-evaluation.md`
