+++
id = "E4-28"
title = "Dashboard: a session view of inference performance, for the record after each class"
labels = ["size:S", "kind:app"]
epic = "E4"
blocked_by = ["E4-29", "E4-33"]
+++

## Context

The inference performance dashboard has a session view that gives the same numbers over one session
window, for the record after each class (section 7.3). The session windows are in the configuration's
schedule (issue E4-33). The dashboards are built from the deployed configuration at synthesis (issue
E2-80), so the session links carry the real schedule, which is private by policy (section 10): it
reaches the image and Grafana, never the repository. In tests the example's invented schedule is
used.

The numbers are the rows of issues E4-12 and E4-29, each summed or taken as a percentile over the
whole window rather than plotted over time: speed per stream (mean, median, slowest tenth), output
throughput, time to first token (warm and held, first turns apart), cached share, the most running
and waiting requests, students per instance, and cost per million output tokens, each percentile
with its request count. The dry-run class of phase 3 is judged against the pools' expected values
(section 4.7; issue E4-26), which the session rows show beside the measurement.

## Deliverables

- [ ] `dashboards/rhi_dashboards/session.py` builds a session row for each pool: the numbers listed above over the dashboard's whole time range, each percentile with its request count, beside the pool's expected values from the configuration.
- [ ] `dashboards/rhi_dashboards/inference.py` adds the session rows to the inference performance dashboard, and one link for each session window of the configuration that opens the dashboard over that window.
- [ ] `tests/unit/dashboards/test_session.py` builds the dashboard from the example configuration and checks the rows and the links, and `tests/unit/dashboards/test_inference.py` tells the session rows apart from the pools' rows.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_session.py` passes: one session row per pool with the numbers listed above, and one link per session window of the example schedule whose time range is that window's start and end.
- [ ] `uv run --locked pytest tests/unit/dashboards/test_build.py tests/unit/dashboards/test_inference.py` passes with the session rows in the dashboard.

## References

- `docs/design/system-design.md`, sections 4.7, 7.3, 10, 14
