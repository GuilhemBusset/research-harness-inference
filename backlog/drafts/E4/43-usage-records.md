+++
id = "E4-43"
title = "Gateway: log each request's route, its cached input tokens, and how long it waited"
labels = ["size:M", "kind:app"]
epic = 6
+++

## Context

Usage is seen, not enforced: there is no per-student token budget, and the counting happens on the
server, from the usage the engine reports, never from what a client sends (section 6.2, step 3;
ADR 0003, decision 2). The instructor reads a session's usage from the gateway's log with CloudWatch
Logs Insights, and from the dashboards.

**What is already there.** The gateway writes one log record per request
(`services/gateway/rhi_gateway/telemetry.py`), whose input_tokens and output_tokens are the counts
the request's settlement observed, 0 for a count the engine did not report. The three usage parsers
of `services/gateway/rhi_gateway/usage/` read the cached part of the input as each format reports it,
0 when it reports none, and the settlement carries it to the record (`RequestRecord.cached_input_tokens`).
A token count (`POST /v1/messages/count_tokens`) is noted on its record with the count it answered
as its input_tokens and output_tokens 0, and the record knows it is one (`RequestRecord.token_count`).
The Prometheus counters of `services/gateway/rhi_gateway/metrics.py` already count the cached input
and the token counts apart. This issue reuses all of that and changes none of it.

**What the log lacks**, and this issue adds to each record:

- **the route**, so that a token count is told apart from a generation in the log as the counters
  already tell them apart: Claude Code sends token counts often, and their input is a count asked,
  not tokens a student generated. The route is the path of the entry of the gateway's route table
  the request matched (`services/gateway/rhi_gateway/routes.py`), and null for a path outside it;
- **the cached part of the input**, which the record holds and does not write today;
- **how long the request waited**: the milliseconds from the request accepted to its first token
  sent, as the time-to-first-token histogram counts them (`services/gateway/rhi_gateway/experience.py`),
  null when no token was sent; and the milliseconds it was held while its pool started, 0 when it
  was not held. The gateway runs the hold (`services/gateway/rhi_gateway/hold.py`) but keeps no
  duration of it today.

The record stays metadata only (section 6.6): no body, no header's value, no key, and the alias as
the only name of a student. Nothing reaches CloudWatch metrics. Whether the engine's Messages usage
also counts the cached part in `input_tokens` (issue #236, question 5) is for the records of issue
#177 or spike S3 to show: the record keeps the counts as the parsers give them.

## Deliverables

- [ ] `services/gateway/rhi_gateway/telemetry.py` writes in each record, beside its present fields, route (the path of the route table's entry the request matched, null for a path outside the table), cached_input_tokens, time_to_first_token_ms (null when no token was sent) and held_ms (0 when the request was not held), and its docstring's table lists the four.
- [ ] `services/gateway/rhi_gateway/app.py` notes on the request's record the time from the request accepted to its first token sent, measured where the time-to-first-token histogram measures it, and the time the request was held, whether or not the metrics are configured.
- [ ] `tests/unit/gateway/test_telemetry.py` covers the four fields on a streamed held request, a warm request, a token count, a request cut before any usage, and a path outside the table.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_telemetry.py` passes: a streamed Messages request that was held gives a record with its route, its input, cached and output tokens as reported, and a time to first token and a time held above 0; a warm request gives held_ms 0; a token count gives its route and its count as input_tokens with output_tokens 0; a request cut before any usage was reported gives 0 tokens and a null time to first token; a path outside the table gives a null route; and no record holds a body, a header's value or a key.
- [ ] `uv run --locked pytest tests/unit/gateway/test_student_metrics.py tests/unit/gateway/test_experience.py tests/unit/gateway/test_time_to_first_token.py tests/unit/gateway/test_hold_path.py` passes.
- [ ] In the account, after a session, a CloudWatch Logs Insights query over the gateway's log group sums each alias's input, cached and output tokens and its token counts apart, by route (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 6.2, 6.6 and 7.2
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 2
- `services/gateway/rhi_gateway/telemetry.py`
- `services/gateway/rhi_gateway/experience.py`
