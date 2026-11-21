+++
id = "E4-44"
title = "AWS Budgets e-mail alerts at thresholds set at deployment, and nothing that stops"
labels = ["size:M", "kind:infra"]
epic = 6
blocked_by = [219]
+++

## Context

The cost net is alerts only (section 11.3; ADR 0003, decision 2): a monthly cost budget that
e-mails the admin at thresholds of a monthly amount, and nothing that stops a pool or a session on
its own. The owner decides what to switch off. The fleet-size cap of `tests/policy/` is not a
budget and is untouched here.

**The amount and the thresholds are configuration, read at deployment.** The monthly amount is the
existing key monthly_budget_usd of `src/rhi/config.py`, whose value is the owner's (section 15,
decision 6) and stays in SSM, never in the repository: the example holds a placeholder. The
thresholds are a new key, budget_alert_percentages, whole percentages above 0, 50, 80 and 100 by
default (section 17), optional so that a configuration in SSM written before this issue loads
unchanged. The CDK app reads both when it synthesizes the budget; no service reads them.

**Where the alerts go.** Every alert of the stage goes to the alerts topic of the guardrails stack,
whose subscriber is the admin's e-mail address and whose key lets AWS Budgets publish (issue #219).
Each threshold is one notification of the budget on actual cost, to that topic. AWS Budgets'
data is updated about once a day (evidence E27), which is enough for an alert.

**What it does not have.** No latch, no second topic, no function: a notification at 100% is an
e-mail like the others. The budget lives in the guardrails stack, which reads resources of other
stacks and is read by none (issue #219).

## Deliverables

- [ ] `src/rhi/guardrails/budget.py` creates a monthly cost budget for the configuration's monthly_budget_usd, with one notification on actual cost at each percentage of budget_alert_percentages, each to the alerts topic, and nothing else: no other topic, no function, no action on any resource.
- [ ] `src/rhi/guardrails/stack.py` adds the budget's construct to the guardrails stack without creating a resource of its own.
- [ ] `src/rhi/config.py` gains budget_alert_percentages, a list of whole percentages above 0 and without repeats, 50, 80 and 100 by default; `config.example.toml` gives it; `tests/unit/test_config.py` holds the default and refuses a percentage of 0 and a repeated one.
- [ ] Every cdk-nag finding `src/rhi/guardrails/budget.py` brings is fixed or acknowledged with its reason in `tests/policy/acknowledged/guardrails_budget.toml`, and `tests/synth/test_budget.py` checks the budget.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_budget.py tests/unit/test_config.py` passes: the guardrails stack holds one budget whose amount is the example configuration's, with one notification on actual cost at each of 50%, 80% and 100%, each to the alerts topic; the stage holds no function and no topic that the budget's notifications reach other than the alerts topic; and a configuration that leaves budget_alert_percentages out loads 50, 80 and 100.
- [ ] The owner writes the monthly amount and, if not the defaults, the thresholds to the configuration in SSM with the configuration step of `setup/owner-setup.sh`, part of decision 6 of the design (needs human:owner-decision).
- [ ] In the account, the admin's address has confirmed the alerts topic's subscription, and the budget lists its thresholds in the Billing console (needs aws:console).

## References

- `docs/design/system-design.md`, sections 11.3, 15 and 17
- `docs/design/evidence.md`, E27
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 2
- `src/rhi/config.py`
