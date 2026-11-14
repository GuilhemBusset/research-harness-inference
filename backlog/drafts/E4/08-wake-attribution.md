+++
id = "E4-08"
title = "Gateway: count the wakes each student causes, and name the latest requester in the heartbeat"
labels = ["size:S", "kind:app"]
epic = "E4"
blocked_by = ["E2-27", "E2-28", "E2-29", "E2-78", "E2-79"]
+++

## Context

Wakes and idle-extension minutes are attributed to the student who caused them (section 11.3). A
leaked or careless key then shows on the students dashboard as GPU time, not only as tokens: one
small request every 19 minutes keeps an instance up for as long as it goes on (section 11.3).
Per-student numbers go to Prometheus only, under the roster alias, never to CloudWatch (section
6.6). The alarm when a pool is in service long outside sessions is issue E4-09.

This issue is the gateway's half. A wake is caused by a request held for a cold pool or by an
explicit `POST /wake` for it (section 5.3): a pool is cold when it has no healthy target and the
scaler has not asked for capacity (issue E2-27). Each gateway task sees its own requests, so the
gateway counts wakes, in its per-student series by alias and pool (issue E2-79). A held request is
the hold's (issue E2-26), an explicit wake the wake route's (issue E2-78); each counts one wake when
its pool was cold on arrival.

Only the scaler sees every gateway task's last request, through their heartbeat rows (issue E2-28),
so the scaler counts the idle-extension minutes (issue E4-39). For that it needs to know whose
request was the latest: this issue adds the alias to the row. A row written before this issue, or
by a task not yet replaced, has no alias, and reads as one.

## Deliverables

- [ ] `services/gateway/rhi_gateway/metrics.py` counts, per alias and pool, the wakes a student caused, and `services/gateway/rhi_gateway/hold.py` and `services/gateway/rhi_gateway/local.py` count one for a request held, or an explicit wake, for a pool that was cold when it arrived; `tests/unit/gateway/test_student_metrics.py` covers it.
- [ ] `services/gateway/rhi_gateway/heartbeat.py` adds to its row, per pool, the alias of the student whose request was received last, and `services/common/rhi_common/records.py` gains the field, a row stored without it reading as no alias; `tests/unit/gateway/test_heartbeat.py` covers it.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_student_metrics.py tests/unit/gateway/test_heartbeat.py` passes: a held request and an explicit wake for a cold pool each count one wake for their student, a request for a ready pool counts none, and the heartbeat row names the alias of the latest request.
- [ ] `uv run --locked pytest tests/unit/common/test_records.py` passes: a gateway heartbeat stored without the alias reads back with no alias.

## References

- `docs/design/system-design.md`, sections 5.1, 5.3, 6.6, 11.3, 17
