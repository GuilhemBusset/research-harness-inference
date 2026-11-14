+++
id = "E4-04"
title = "AWS Budgets as the backstop: alerts at 50 and 80%, every pool latched at 100%"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E1-16", "E2-12", "E4-36"]
+++

## Context

AWS Budgets' data is updated about once a day, so it is a brake, not a cap; at 100% it latches every
pool (section 11.3; evidence E27). A latched pool is in mode `budget-latched`, which only the admin
clears (issue E4-03), and a request for it is answered `pool_ceiling_reached` (section 6.2, step 8).
Notifications go out at 50%, 80% and 100% of the monthly amount by default (section 17). The monthly
amount is a key of the configuration since issue E1-03, and its value is the owner's decision 6.

The thresholds below 100% are configuration: the key budget_alert_percentages, 50 and 80 by default
(section 17), so that a configuration in SSM written before this issue loads unchanged. The CDK app
reads it when it synthesizes the budget; no service reads it. The 100% notification is not
configurable: it is the latch.

The alerts topic, its key and the guardrails stack come from issue E4-36. AWS Budgets gives each
notification one topic, so the 100% notification goes to a latch topic of its own, encrypted with the
alerts topic's key, whose one subscriber is the function that latches; that function then tells the
alerts topic what it did. The function is part of the scaler's package and is packaged as issue
E1-16 packages every function. It lives in the guardrails stack, which may read the pool-state
table of another stack; no stack reads the guardrails stack (issue E4-36).

## Deliverables

- [ ] `src/rhi/guardrails/budget.py` creates a monthly cost budget for the configuration's monthly amount, with a notification to the alerts topic at each percentage of budget_alert_percentages and one at 100% to a latch topic, encrypted with the alerts topic's key, whose one subscriber is the latch function; both topics' policies let AWS Budgets publish and name no account other than the stack's own.
- [ ] `src/rhi/config.py` gains budget_alert_percentages, whole percentages below 100, 50 and 80 by default; `config.example.toml` gives them; `tests/unit/test_config.py` holds the default and refuses a percentage of 100 or more.
- [ ] `services/scaler/rhi_scaler/latch.py` is the latch function: it sets every configured pool's mode to budget-latched, with the budget as who set it, then publishes one message to the alerts topic that says the pools are latched, and does nothing else; `tests/unit/scaler/test_latch.py` covers it.
- [ ] `src/rhi/guardrails/stack.py` adds the budget's construct of `src/rhi/guardrails/budget.py` to the guardrails stack, without creating a resource of its own.
- [ ] `src/rhi/guardrails/budget.py` deploys the latch function with the pools' names, the pool-state table and the alerts topic in its environment, and a role that may write the pool-state table's items and publish to the alerts topic, and nothing else; `tests/synth/test_budget.py` checks the budget, the topics and the role.
- [ ] Every cdk-nag finding `src/rhi/guardrails/budget.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/guardrails_budget.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_budget.py tests/unit/test_config.py` passes: the guardrails stack of the app holds the budget, its amount is the example's, it notifies the alerts topic at 50% and 80% and the latch topic at 100%, the latch function is the latch topic's only subscriber, its role is limited as stated, and a configuration that leaves budget_alert_percentages out loads 50 and 80.
- [ ] `uv run --locked pytest tests/unit/scaler/test_latch.py` passes: after the function every pool is budget-latched with the budget as who set it and the alerts topic received one message, and a second call leaves the records as they were.
- [ ] The owner sets the monthly amount, part of decision 6 of the design, and writes it to the configuration in SSM (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 6.2, 7.3, 11.3, 15, 17
- `docs/design/evidence.md`, E27
