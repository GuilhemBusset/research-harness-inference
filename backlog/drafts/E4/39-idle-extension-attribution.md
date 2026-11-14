+++
id = "E4-39"
title = "Scaler: charge idle-extension minutes to the student whose request caused them"
labels = ["size:S", "kind:app"]
epic = "E4"
blocked_by = ["E2-11", "E4-08"]
+++

## Context

Wakes and idle-extension minutes are attributed to the student who caused them (section 11.3); the
wakes are the gateway's count (issue E4-08), the minutes are this issue's. Per-student numbers go to
Prometheus only, under the roster alias, never to CloudWatch (section 6.6).

An idle extension is the time a request pushes back a pool's scale-in: the pool goes to zero
`idle_minutes` after the last request received, 20 by default (sections 5.1 and 17), so a request
received 15 minutes into an idle period pushes it back 15 minutes. Only the scaler sees every
gateway task's last request, through their heartbeat rows, which name the alias of the latest
request since issue E4-08. The scaler sees the latest request of each task at each run, which is the
precision this needs. A row without an alias charges nobody.

## Deliverables

- [ ] `services/scaler/rhi_scaler/attribution.py` charges, on each run, when a pool's latest request received is newer than at the previous run and the pool had nothing in flight at the previous run, the student of that request with the minutes between the two requests, at most the pool's idle_minutes.
- [ ] `services/scaler/rhi_scaler/metrics.py` sends the minutes charged per alias to the Prometheus workspace, and no alias goes into a log record of the scaler or anywhere else; `tests/unit/scaler/test_attribution.py` covers both.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_attribution.py` passes: a request received 15 minutes into an idle period of a pool with idle_minutes 20 charges its student 15 minutes, a request received while another is in flight charges nothing, a row without an alias charges nobody, and no charge exceeds 20 minutes.
- [ ] `tests/unit/scaler/test_attribution.py::test_the_alias_goes_to_prometheus_only` shows the charged alias in the series sent to the workspace and in no log record of the run.

## References

- `docs/design/system-design.md`, sections 5.1, 6.6, 11.3, 17
