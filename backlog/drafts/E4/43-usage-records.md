+++
id = "E4-43"
title = "Gateway: log each request's route, its token counts as the engine reported them with the cached part, and how long it waited"
labels = ["size:M", "kind:app"]
epic = 6
+++

## Context

Usage is seen, not enforced: there is no per-student token budget, and the counting happens on the
server, from the usage the engine reports, never from what a client sends (section 6.2, step 3;
ADR 0003, decision 2). The gateway already writes one log record per request
(`services/gateway/rhi_gateway/telemetry.py`) and counts tokens per student in Prometheus
(`services/gateway/rhi_gateway/metrics.py`). This issue makes those records say what the instructor
needs to read a session's usage from the log and the dashboards:

- **the route**, so that a token count is told apart from a generation: Claude Code sends
  `POST /v1/messages/count_tokens` often, and its input is a count asked, not tokens a student
  generated. A token count's answer is a JSON object whose `input_tokens` is the count;
- **the cached part of the input**, which each format reports apart and the usage parsers do not
  read today: `cache_read_input_tokens` on Messages, `prompt_tokens_details.cached_tokens` on Chat
  Completions, `input_tokens_details.cached_tokens` on Responses. A response that reports no cached
  part gives 0. Whether the engine's Messages usage also counts the cached part in `input_tokens`
  (issue #236, question 5) is for the records of issue #177 or spike S3 to show; the record keeps
  the fields as the engine reports them, summed as the parsers already sum the input;
- **how long the request waited**: the milliseconds to its first token sent, and the milliseconds
  it was held while its pool started. The gateway already measures both for its histograms
  (`services/gateway/rhi_gateway/experience.py`).

The token counts logged are those the engine reported, 0 for one it did not report: never an
estimate. The record stays metadata only (section 6.6): no body, no header's value, no key, and the
alias as the only name of a student. Nothing reaches CloudWatch metrics.

## Deliverables

- [ ] The parsers of `services/gateway/rhi_gateway/usage/` each also read the cached part of the input as their format reports it, 0 when the response reports none.
- [ ] `services/gateway/rhi_gateway/telemetry.py` gives each record the fields route (the path of the route in the gateway's table, null for a path outside it), cached_input_tokens, time_to_first_token_ms (null when no token was sent) and held_ms (0 when the request was not held), and its input_tokens and output_tokens are the counts the engine reported, 0 for a count it did not report.
- [ ] `services/gateway/rhi_gateway/app.py` notes a token count's answered count on its record as its input_tokens, with output_tokens 0.
- [ ] `services/gateway/rhi_gateway/metrics.py` counts, per alias and pool, the cached input tokens in rhi_gateway_student_cached_input_tokens_total and the tokens of token counts in rhi_gateway_student_counted_tokens_total, and counts a token count in neither rhi_gateway_student_input_tokens_total nor rhi_gateway_student_output_tokens_total.
- [ ] `tests/unit/gateway/test_telemetry.py`, `tests/unit/gateway/test_student_metrics.py` and the tests of each usage parser cover the new fields and series.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_usage_messages.py tests/unit/gateway/test_usage_chat.py tests/unit/gateway/test_usage_responses.py` passes: each format's cached part is read from a recorded response, and a response without one gives 0.
- [ ] `uv run --locked pytest tests/unit/gateway/test_telemetry.py` passes: a streamed Messages request that was held gives a record with its route, its input, cached and output tokens as reported, a time to first token and a time held above 0; a token count gives its route and its count as input_tokens with output_tokens 0; a request cut before any usage was reported gives 0 tokens; and no record holds a body, a header's value or a key.
- [ ] `uv run --locked pytest tests/unit/gateway/test_student_metrics.py` passes: after one generation and one token count by the same alias, the input and output counters hold the generation's tokens only, the cached counter its cached part, and the counted-tokens counter the count.
- [ ] In the account, after a session, a CloudWatch Logs Insights query over the gateway's log group sums each alias's input, cached and output tokens and its token counts apart (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 6.2, 6.6 and 7.2
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 2
- `services/gateway/rhi_gateway/telemetry.py`
- `services/gateway/rhi_gateway/metrics.py`
