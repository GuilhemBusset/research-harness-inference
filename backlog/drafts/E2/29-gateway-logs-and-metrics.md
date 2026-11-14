+++
id = "E2-29"
title = "Gateway: one log record per request, metadata only, and its own faults told apart"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-26", "E2-78"]
+++

## Context

Prompts and responses are not stored (section 6.6). The gateway logs metadata only: who, when, which
pool, token counts, latency, status; never a request or a response body (section 6.2).

The gateway's log records go to CloudWatch Logs, which keeps them for a set retention: the gateway
writes each record to its standard output as one JSON object per line, and the task's log
configuration sends that output to the gateway's log group (issue E2-30). CloudWatch metrics cannot
be deleted for 15 months, so nothing per student goes there (section 6.6; evidence E27), and a log
record in CloudWatch's embedded metric format would turn into CloudWatch metrics, so none is.

The objective "99.5% of requests are not 5xx from the gateway's own fault" (section 7.3) needs the
gateway's own faults told apart from the causes it gives on purpose. An unhandled exception is such
a fault: it is answered 500 with no text of its own, and recorded under the cause `none`. The
per-student counters of the same answers are issue E2-79.

## Deliverables

- [ ] `services/gateway/rhi_gateway/telemetry.py` writes one log record per request to standard output, one JSON object per line, with the student's alias, the time, the pool, the wire format, the input and output tokens, the latency, the status and the cause if any.
- [ ] No log record holds a body, a header's value, a key or its hash, or a GitHub ID, and none is in CloudWatch's embedded metric format.
- [ ] An answer of 500 or above that names no cause of issue E2-02, such as an unhandled exception, is a 500 that carries none of the exception's text, and its log record carries the cause none.
- [ ] `services/gateway/rhi_gateway/app.py` records every request the gateway answers, the local routes and refusals included, and `tests/unit/gateway/test_telemetry.py` covers the records.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_telemetry.py` passes: a request gives one record with exactly the stated fields, and a refused request and a local route each give one too.
- [ ] `tests/unit/gateway/test_telemetry.py::test_nothing_of_a_request_is_logged` sends a body and headers that hold a marker string and finds the marker in no log record.
- [ ] `tests/unit/gateway/test_telemetry.py::test_an_unhandled_error_is_cause_none` raises an exception in a handler and finds a 500 without the exception's text and a log record with the cause none.
- [ ] `tests/unit/gateway/test_telemetry.py::test_no_embedded_metric_format` finds no embedded-metric-format key in any log record of a run that covers every route.

## References

- `docs/design/system-design.md`, sections 6.2, 6.6, 7.3
- `docs/design/evidence.md`, E27
