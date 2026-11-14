+++
id = "E4-36"
title = "The guardrails stack: the alerts topic, and Cost Anomaly Detection behind a flag that is off by default"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E1-05", "E1-07", "E1-13"]
+++

## Context

Every alarm of the stage publishes to one alerts topic (section 7.3), and the owner confirms its
e-mail subscription once (section 8.6, step 5). The pipeline's own topic for the owner is another
one (issue E0-22). This issue creates the guardrails stack and that topic, which the budget (issue
E4-04) and the alarms (issues E4-09 and E4-10) use.

**The guardrails stack reads, and nothing reads it.** Its alarms and its latch function read
resources of the other stacks of the stage (the pools' groups, the pool-state table, the gateway's
log group, the public load balancer), so no other stack may read a resource of the guardrails
stack: that would be a cycle between two stacks. A topic that a construct of another stack publishes
to lives in that stack (the class-day topic, issue E4-06).

Each module of the guardrails stack defines a construct class, so that a cdk-nag finding on one of
its resources belongs to that module's acknowledgement file (issue E1-05), and the stack module
creates no resource of its own.

**The topic's key.** cdk-nag asks for encrypted topics (issue E1-05). The services that publish to
the alerts topic (CloudWatch alarms, AWS Budgets and Cost Anomaly Detection) cannot use the
AWS-managed key of SNS, so the topic is encrypted with a key of the stack, rotated every year, whose
policy lets those three services use it. Other topics of the stack, such as the budget's latch
topic, may use the same key.

**Cost Anomaly Detection, behind a flag.** Detection is Cost Anomaly Detection, beside the cost
dashboard (section 11.3). Its findings go to the alerts topic at once, whatever their size: the
design sets no threshold. But an account may hold only one AWS-managed monitor of the services
dimension, and Cost Explorer creates one for a new user, so creating ours would fail the deployment
in such an account, and with it the merge. So the monitor and its subscription are built only when
the key cost_anomaly_detection is true, false by default: the owner first removes an existing
monitor, then turns the flag on. The design gives no such key; false keeps the deployed stage
deployable, and the owner's item turns detection on. The CDK app reads the key when it synthesizes
the stack.

## Deliverables

- [ ] `src/rhi/guardrails/alerts.py` creates the alerts topic, encrypted with a key of the stack that rotates every year and whose policy lets CloudWatch, AWS Budgets and Cost Anomaly Detection use it, refusing unencrypted transport, with an e-mail subscription to the admin's address from the configuration.
- [ ] `src/rhi/guardrails/stack.py` is the guardrails stack, which holds the topic's construct and creates no resource of its own, so that it brings no cdk-nag finding and needs no acknowledgement file, and `src/rhi/stage.py` adds it to the stage.
- [ ] `src/rhi/guardrails/anomaly.py` creates, only when cost_anomaly_detection is true, a Cost Anomaly Detection monitor of the services dimension and a subscription that sends every anomaly to the alerts topic at once, with a total-impact threshold of 0 dollars, and lets Cost Anomaly Detection publish to the topic, naming no account other than the stack's own.
- [ ] `src/rhi/config.py` gains cost_anomaly_detection, false by default; `config.example.toml` gives it false; `tests/unit/test_config.py` holds the default.
- [ ] `tests/synth/test_alerts.py` checks the topic, its key and its subscription, and the monitor both ways.
- [ ] Every cdk-nag finding `src/rhi/guardrails/alerts.py`, `src/rhi/guardrails/anomaly.py` or `src/rhi/stage.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/guardrails_alerts.toml`, `tests/policy/acknowledged/guardrails_anomaly.toml` or `tests/policy/acknowledged/stage.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_alerts.py tests/unit/test_config.py` passes: under the example configuration the topic has one e-mail subscription, to the example's address, and is encrypted with a rotating key whose policy names the three services, there is no anomaly monitor, and a configuration that leaves cost_anomaly_detection out loads false.
- [ ] `tests/synth/test_alerts.py::test_anomaly_detection_behind_its_flag` synthesizes with the flag on and finds one monitor of the services dimension and one subscription that sends at once to the alerts topic.
- [ ] The owner confirms the alerts topic's e-mail subscription, step 5 of section 8.6 (needs aws:console).
- [ ] The owner removes the account's AWS-managed monitor of the services dimension in Cost Explorer, if there is one (needs aws:console).
- [ ] The owner then sets cost_anomaly_detection to true in the configuration in SSM with the configuration step of `setup/owner-setup.sh`, and the next deployment creates the monitor (needs aws:account).

## References

- `docs/design/system-design.md`, sections 7.3, 8.6, 11.3, 17
