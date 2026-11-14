+++
id = "E2-79"
title = "Gateway: per-student metrics by roster alias, served on localhost for the collector beside it"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-15", "E2-29", "E2-58"]
+++

## Context

Per-student numbers exist only in Prometheus, labelled with the roster alias and not the GitHub ID
(sections 6.6 and 7.2): requests, tokens in and out, errors, and the budget left. CloudWatch metrics
cannot be deleted for 15 months, so nothing per student goes there (section 6.6; evidence E27).

The gateway exposes its metrics on localhost, where the collector beside it in the task scrapes them
and pushes them to Prometheus (section 7.1). The port is 9091, which the gateway's collector
configuration of issue E2-58 scrapes; the design names no port. The per-student request counter
carries the pool, so that the students with a request in the last five minutes, per pool, which the
inference dashboard sets against the pool's instances in service (section 7.2), are a count of
aliases over it, correct across the gateway's tasks. The errors are counted by cause, the gateway's
own faults under the cause `none` (issue E2-29), which the error-rate alarm reads (section 7.3).
What students experience, per pool and never per student, is issues E2-74 and E2-84.

The budget left is the ledger's read of issue E2-15. The Prometheus client library is pinned by
issue E1-16.

## Deliverables

- [ ] `services/gateway/rhi_gateway/metrics.py` serves the gateway's metrics in Prometheus format on 127.0.0.1, port 9091, and on no other address.
- [ ] Per alias and pool it counts requests, input tokens, output tokens, and errors by cause, the gateway's own faults under the cause none; per alias it gives the budget left of the day, from the ledger's read of issue E2-15.
- [ ] Every per-student series is labelled by the roster alias and by no other identity of the student, and the gateway publishes no CloudWatch metric.
- [ ] `services/gateway/rhi_gateway/app.py` counts every request the gateway answers, the local routes and refusals included, and `tests/unit/gateway/test_student_metrics.py` covers the metrics.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_student_metrics.py` passes: the metrics of two students are separate, each counter equals the requests and tokens the test sent, and the budget left equals the student's budget less what was charged.
- [ ] `tests/unit/gateway/test_student_metrics.py::test_students_are_aliases` finds the alias, and never the GitHub ID or the key of the test's student, in every per-student series.
- [ ] `tests/unit/gateway/test_student_metrics.py::test_errors_by_cause` counts a refusal under its cause and an unhandled exception under the cause none.
- [ ] `tests/unit/gateway/test_student_metrics.py::test_the_collector_scrapes_this_port` reads `services/gateway/collector/config.yaml` and finds its one scrape target at 127.0.0.1, port 9091.

## References

- `docs/design/system-design.md`, sections 6.6, 7.1, 7.2, 7.3
- `docs/design/evidence.md`, E27
