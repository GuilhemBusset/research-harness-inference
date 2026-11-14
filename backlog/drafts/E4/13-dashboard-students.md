+++
id = "E4-13"
title = "Dashboard: students, by roster alias"
labels = ["size:S", "kind:app"]
epic = "E4"
blocked_by = ["E2-61", "E2-79", "E4-39"]
+++

## Context

One of the six dashboards of section 7.3: per student, requests, tokens in and out, errors and the
budget left, from the gateway (section 7.2; issue E2-79), plus the wakes caused (issue E4-08) and
the idle minutes extended (issue E4-39), so that a leaked or careless key shows as GPU time, not
only as tokens (section 11.3). Students appear under their roster alias, never their GitHub ID: per-student series
exist only in Prometheus, labelled with the alias (section 6.6).

The gateway runs two tasks and the collector labels each task's series apart (issue E2-58), so a
student's numbers are summed over the tasks, after rates are taken per task (section 7.2, first
rule; issue E2-61). The budget counts every input token, cached or not, against a default of 100
million tokens a day (section 11.3).

## Deliverables

- [ ] `dashboards/rhi_dashboards/students.py` defines the students dashboard: per alias, from the gateway and each summed over its tasks, requests, input and output tokens, errors by cause, the budget left of the day and the wakes caused; and, from the scaler, the idle minutes extended.
- [ ] `tests/unit/dashboards/test_students.py` builds the dashboard from the example configuration and checks its panels.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_students.py` passes: the seven quantities each have a panel, and every query groups by the alias label and by no other identity of a student.
- [ ] `uv run --locked pytest tests/unit/dashboards/test_build.py` passes with the students dashboard in the package.

## References

- `docs/design/system-design.md`, sections 6.6, 7.2, 7.3, 11.3
