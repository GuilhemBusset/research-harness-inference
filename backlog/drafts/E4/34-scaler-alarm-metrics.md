+++
id = "E4-34"
title = "Scaler: publish to CloudWatch what the alarms need, and nothing about a student"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-12", "E4-02", "E4-05"]
+++

## Context

Alerts that must work when Grafana is down are CloudWatch alarms (section 7.3). The scaler sends its
own numbers to Prometheus (section 7.2); a CloudWatch alarm needs them in CloudWatch. Five of the
alarms read what only the scaler knows:

- the scaler has not completed a run for five minutes (section 5.1);
- a pool's launches are suspended after three abandoned launches (sections 5.1 and 5.4);
- a pool has reached a GPU-hour ceiling (section 7.3; issue E4-02);
- a pool has been in service long outside sessions, 3 hours by default (sections 11.3 and 17).
  Outside sessions means outside every session window widened by the pool's pre-warm lead (issue
  E4-05): a pre-warmed pool is in a session;
- the class-day notice has decided a no-go (section 7.3; issue E4-06), which this issue's publisher
  sends when the notice hands it one.

The names, so that the alarms of issues E4-09 and E4-10 read what this issue writes: the namespace
rhi/scaler; the data points RunCompleted (no dimension), LaunchesSuspended and AtGpuHourCeiling (1
or 0, with the dimension Pool), MinutesInServiceOutsideSessions (with the dimension Pool), and
ClassDayNoGo (no dimension, sent only for a no-go). The pool's name is the only dimension any of them
carries.

CloudWatch metrics cannot be deleted for 15 months, so nothing per-student goes there (section 6.6;
evidence E27). CloudWatch grants the right to publish metrics only on every resource; the scaler's
role gets it with a condition that limits it to the one namespace, and the module's acknowledgement
file lists the action with that reason.

## Deliverables

- [ ] `services/scaler/rhi_scaler/outside_sessions.py` computes, per pool, the minutes it has had an instance in service without a break while outside every session window widened by the pool's pre-warm lead, and restarts the count when the pool has none in service or a widened window begins; `tests/unit/scaler/test_outside_sessions.py` covers the count.
- [ ] `services/scaler/rhi_scaler/cloudwatch.py` publishes, in the namespace rhi/scaler and at the end of every completed run, RunCompleted and, per pool, LaunchesSuspended, AtGpuHourCeiling and MinutesInServiceOutsideSessions, and ClassDayNoGo when it is handed a no-go, with no dimension other than the pool; `tests/unit/scaler/test_cloudwatch.py` covers it against a stubbed client.
- [ ] `services/scaler/rhi_scaler/handler.py` calls it after a run has applied its decision, and a run that fails publishes nothing.
- [ ] `src/rhi/pools/scaler.py` lets the scaler's role publish metrics to the namespace rhi/scaler and to no other, and `tests/synth/test_scaler.py` checks it.
- [ ] Every cdk-nag finding `src/rhi/pools/scaler.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/pools_scaler.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_outside_sessions.py` passes: a pool in service for three hours and one minute from a window's end, with no other window that day, counts 181 minutes, minutes inside a window or its lead count nothing, and the count restarts after the pool has gone to zero.
- [ ] `uv run --locked pytest tests/unit/scaler/test_cloudwatch.py` passes: a completed run publishes RunCompleted and the three values per pool, a no-go handed to it is published as ClassDayNoGo, a run that raises publishes nothing, and no published metric has a dimension other than the pool.
- [ ] `uv run --locked pytest tests/synth/test_scaler.py tests/policy/test_iam_wildcards.py` passes with the role allowed to publish to the one namespace only, under a condition on the namespace.

## References

- `docs/design/system-design.md`, sections 5.1, 5.4, 6.6, 7.2, 7.3, 11.3, 17
- `docs/design/evidence.md`, E27
