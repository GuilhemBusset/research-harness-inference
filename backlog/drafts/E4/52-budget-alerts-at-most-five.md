+++
id = "E4-52"
title = "The configuration refuses more than five budget alerts, the most one AWS budget takes"
labels = ["size:S", "kind:infra"]
epic = 6
+++

## Context

#589 reads the AWS Budgets alert thresholds from the configuration, `budget_alert_percentages`,
and accepts from 1 to 10 of them (`MOST_BUDGET_ALERTS` in `src/rhi/config.py`;
`config.example.toml` says "1 to 10 ... what one budget can hold"). AWS Budgets takes at most
five notifications on one budget (the CreateBudget API and the AWS Budgets documentation; the
CloudFormation resource's schema lists ten, which is not what the service accepts). So a
configuration with six to ten thresholds passes the configuration's checks and the synthesis,
and fails only when the stack is deployed, and the example file tells the owner something
false (follow-up #626).

## Deliverables

- [ ] `src/rhi/config.py` accepts one to five budget alert percentages, refuses six or more with an error that names budget_alert_percentages, and its comment says why five.
- [ ] `config.example.toml` says one to five budget alert percentages, and nothing in it or in `src/rhi/config.py` still says one to ten.

## Acceptance criteria

- [ ] `tests/unit/test_config.py` holds that six distinct percentages are refused with an error naming budget_alert_percentages and that five distinct percentages load.
- [ ] `uv run --locked pytest tests/unit/test_config.py tests/synth/test_alerts.py` passes.
- [ ] `git grep -n "1 to 10" -- config.example.toml src/rhi/config.py` finds nothing.

## References

- `src/rhi/config.py`
- `config.example.toml`
- `docs/design/system-design.md`, section 11.3
