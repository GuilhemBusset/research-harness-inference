+++
id = "E4-44"
title = "AWS Budgets alerts at thresholds read from the configuration"
labels = ["size:S", "kind:infra"]
epic = 6
blocked_by = [219]
+++

## Context

The cost net is alerts only (section 11.3; ADR 0003, decision 2): a monthly cost budget that
e-mails the admin, through the alerts topic, at thresholds of a monthly amount, and nothing that
stops a pool or a session on its own. The design makes both the amount and the thresholds
configuration, read when the stage is deployed (section 11.3), 50%, 80% and 100% by default
(section 17).

**What is already there.** The guardrails stack holds the budget (`src/rhi/guardrails/budget.py`):
a monthly cost budget for the configuration's monthly_budget_usd, one notification on actual cost
at each threshold, each to the alerts topic, the topic policy's statement that lets AWS Budgets
publish, and no latch, function or second topic. `tests/synth/test_alerts.py` checks it. Its
thresholds are fixed in the module (`THRESHOLDS`), not read from the configuration.

**What this issue changes.** The thresholds become the configuration's key
budget_alert_percentages: whole percentages above 0, without repeats, 50, 80 and 100 by default.
The key is optional, so that a configuration in SSM written before this issue loads unchanged and
gives the defaults. The CDK app reads it when it synthesizes the budget; no service reads it. The
fleet-size cap of `tests/policy/` is not a budget and is untouched here.

## Deliverables

- [ ] `src/rhi/config.py` gains budget_alert_percentages, a list of whole percentages above 0 without repeats, 50, 80 and 100 when the key is left out, and `config.example.toml` gives it.
- [ ] `src/rhi/guardrails/budget.py` makes one notification on actual cost, to the alerts topic, at each percentage of the configuration's budget_alert_percentages, and holds no fixed list of thresholds.
- [ ] `tests/synth/test_alerts.py` checks the notifications against the configuration, and `tests/unit/test_config.py` holds the default and refuses a percentage of 0 and a repeated one.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_alerts.py tests/unit/test_config.py` passes: under the example configuration the budget notifies the alerts topic at 50%, 80% and 100% of the example's amount; a configuration with budget_alert_percentages of 25 and 90 gives exactly those two notifications; a configuration that leaves the key out loads 50, 80 and 100; and a list holding 0 or a repeated percentage is refused.
- [ ] `uv run --locked pytest tests/synth/test_nag.py tests/synth/test_parked.py` passes.
- [ ] In the account, after the deployment, the budget lists its thresholds in the Billing console (needs aws:console).

## References

- `docs/design/system-design.md`, sections 11.3 and 17
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 2
- `src/rhi/guardrails/budget.py`
- `src/rhi/config.py`
