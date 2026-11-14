+++
id = "E2-02"
title = "A fixed vocabulary of twelve causes, rendered as an error in each wire format"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-02"]
+++

## Context

Students paste errors into public issues, so the gateway never relays text from AWS or from the
engine (section 5.3). Every error the gateway produces is in the wire format in use and names one
cause from a fixed list, with a fixed sentence (section 6.2, step 8):

| Cause | Status | When |
| --- | --- | --- |
| `invalid_key` | 401 | The key is missing, malformed, unknown, revoked or expired, or the two key headers differ |
| `not_found` | 404 | Any route outside the table |
| `request_refused` | 400 | The filter rejects the body: not JSON, duplicate keys, too large, a media URL that is not inline data |
| `concurrency_limit` | 429 with `Retry-After` | The student is at their limit of concurrent requests |
| `budget_reached` | 402 | The student's daily token budget is spent |
| `pool_ceiling_reached` | 402 | The pool is at its GPU-hour ceiling, or the monthly budget has latched it |
| `warming` | 503 with `Retry-After` | The hold deadline passed while the pool was starting; also token counting on a cold pool |
| `capacity_unavailable` | 503 with `Retry-After` | AWS could not provide an instance |
| `pool_scheduled_only` | 503 | A student asked for a pool that only a schedule or the admin starts |
| `pool_disabled` | 503 | The admin disabled the pool |
| `engine_error` | 502 | The engine failed, or ended a response early. On a stream that has started, this is that format's error event |
| `time_limit` | 504 | The request reached the 30-minute limit. On a stream, that format's error event |

The SDKs retry 408, 409, 429 and 5xx, and nothing else (evidence E12), so the two budget causes use
402 and reach the student at once. `Retry-After` is 30 seconds for `warming`, 60 for
`capacity_unavailable` and 5 for `concurrency_limit`; the three values are defaults of section 17.
This issue gives them as the enumeration's defaults, which a caller may replace. The configuration
key that replaces them, and the way it reaches the gateway's tasks, belong to the gateway's settings
(issue E2-76), so this issue needs no configuration.
Each of the three wire formats has its own error shape, for a JSON answer and for the event that
ends a stream, and a client only understands its own. A stream that was already answered 200 cannot
change its status: its cause travels in that format's error event. A parked platform (section 11.2)
has no gateway and answers nothing, so no cause is for it.

## Deliverables

- [ ] `services/common/rhi_common/causes.py` defines the twelve causes of section 6.2 as an enumeration, each with the HTTP status of that table, the seconds of its Retry-After header if it carries one, and one fixed sentence.
- [ ] The statuses are 401 for invalid_key, 404 for not_found, 400 for request_refused, 429 for concurrency_limit, 402 for budget_reached and for pool_ceiling_reached, 503 for warming, capacity_unavailable, pool_scheduled_only and pool_disabled, 502 for engine_error and 504 for time_limit.
- [ ] Retry-After is carried by warming, capacity_unavailable and concurrency_limit and by no other cause, 30, 60 and 5 seconds by default, and the renderer takes other values for these three when its caller gives them.
- [ ] `services/gateway/rhi_gateway/errors.py` renders an error in each wire format (Anthropic Messages, OpenAI Responses, OpenAI Chat Completions), as a JSON answer with the cause's status and as the event that ends a stream of that format.
- [ ] A rendered error names its cause and carries that cause's fixed sentence, and no text from an exception, from the engine or from AWS.
- [ ] `tests/unit/gateway/test_errors.py` covers every cause in every format, streamed and not.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_errors.py` passes: each of the twelve causes renders to the expected JSON with its status, and to the expected final event, in each of the three formats.
- [ ] `tests/unit/gateway/test_errors.py::test_statuses_are_the_designs` checks the twelve statuses, that warming, capacity_unavailable and concurrency_limit carry a Retry-After of 30, 60 and 5 seconds by default and no other cause carries one, and that a value the caller gives replaces the default.
- [ ] `tests/unit/gateway/test_errors.py::test_no_foreign_text` renders errors from exceptions whose messages hold an address and an ARN built in the test, and finds neither in any output.

## References

- `docs/design/system-design.md`, sections 5.3, 6.2, 17
- `docs/design/evidence.md`, E12
