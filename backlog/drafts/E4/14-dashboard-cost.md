+++
id = "E4-14"
title = "Dashboard: cost, labelled an estimate and reconciled with the bill"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-60", "E2-61", "E2-79", "E4-02"]
+++

## Context

One of the six dashboards of section 7.3: GPU-hours and estimated dollars per pool, and cost per
million tokens. The dollars are the scaler's accrued instance-seconds times the configured hourly
price (issue E4-01); the tokens are the gateway's output tokens settled (issue E2-79). Every
number computed so is labelled an estimate, and one billing panel reconciles it against the
account's month-to-date charge (section 7.2). The cost per million tokens is an instance's price over what it produced,
the output tokens (section 4.7), for a session and for the month.

The account's month-to-date charge is CloudWatch's billing metric. AWS publishes it in the US East
(N. Virginia) region only, whatever the deployment's region, and only once billing alerts are on,
which is the owner's fifth one-time step (section 8.6). Grafana's role may read CloudWatch metrics
(issue E2-60).

The GPU-hour ceilings are 16 a day and 150 a month for the coursework pool (section 17), and the
always-on platform costs about $170 a month at idle (section 11.2); the dashboard shows the ceilings
beside the hours, from the configuration, where issue E4-02 adds the monthly one. Cost Anomaly
Detection is issue E4-36.

## Deliverables

- [ ] `dashboards/rhi_dashboards/cost.py` defines the cost dashboard: per pool, the GPU-hours of the day and of the month beside the pool's daily and monthly ceilings from the configuration, their estimated cost, and the estimated cost per million output tokens over the dashboard's time range and for the month.
- [ ] Every panel computed from the scaler's numbers says estimate in its title.
- [ ] One panel shows the account's month-to-date charge from CloudWatch's billing metric, queried in the region where AWS publishes it, beside the month's estimate.
- [ ] `tests/unit/dashboards/test_cost.py` builds the dashboard from the example configuration and checks its panels.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/dashboards/test_cost.py` passes: each panel computed from the scaler's numbers has estimate in its title, the ceilings are the example configuration's, and exactly one panel reads the billing metric, in the US East (N. Virginia) region.
- [ ] `uv run --locked pytest tests/unit/dashboards/test_build.py` passes with the cost dashboard in the package.
- [ ] In the account, the billing panel shows the month-to-date charge (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.7, 7.2, 7.3, 8.6, 11.2, 11.3, 17
