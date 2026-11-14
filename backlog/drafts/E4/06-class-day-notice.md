+++
id = "E4-06"
title = "A go or no-go notice thirty minutes before each session, to the instructor's e-mail and phone"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E4-05", "E4-34"]
+++

## Context

Thirty minutes before a session the scaler sends the instructor a go or no-go notice with each
pool's state (section 5.4), and the notice also goes to the instructor's phone (section 7.3). A
class-day no-go is also one of the alarms that must work when Grafana is down (section 7.3). The
objective it reports on: every scheduled pool in service 30 minutes before the session (section
7.3). The runbook then has one fallback command, switching the default model to a pool that is up
(section 5.4; issues E4-22 and E4-27).

The design does not define go beyond each pool's state. Here a session is go when every pool its
window names has in service at least the number of instances the window names, which is what the
scaler pre-warms (issue E4-05); otherwise it is no-go, and the notice says, for each pool, how many
instances are in service against the number named, and its mode.

Three outputs, each with its reader:

- The notice goes to the class-day topic, with an e-mail and a text-message subscription.
- A no-go is also a CloudWatch data point, through the publisher of issue E4-34; the alarm on it,
  to the alerts topic, is issue E4-10's.
- The verdict of every window goes to Prometheus as a series that is 1 for go and 0 for no-go, at
  30 minutes before the window's start: the objective "pool ready at session start" reads it
  (issue E4-16).

**Which stack holds the class-day topic.** The guardrails stack reads resources of the other stacks
(the pool-state table, the pools' groups, the gateway's log group) and no stack reads it (issue
E4-36). The scaler, in the pools stack, publishes to the class-day topic, so the topic cannot live
in the guardrails stack without a cycle between the two stacks: it lives in the pools stack, in a
module of its own beside the scaler, and the scaler's construct lets the function publish to it and
adds its ARN to the function's settings (issue E2-11). Settings without it read as no topic, and
then no notice is published. Only the scaler publishes to it, so the AWS-managed key of SNS can
encrypt it. The grant lands in the scaler's role, a resource of the scaler's construct, so a cdk-nag
finding on it belongs to the scaler module's acknowledgement file (issue E1-05).

The pools stack passes the topic to the scaler's construct, so the class-day module stays
independent of the scaler; when the configuration is parked the scaler is not built and the topic
stays (issue E4-17).

The instructor is the only admin (assumption A1). The admin's e-mail address and the instructor's
phone number are keys of the configuration since issue E1-03, kept in SSM, never in the repository;
the example holds placeholders only (section 10). Sending text messages from AWS needs account-side
setup that only the owner can do.

## Deliverables

- [ ] `services/scaler/rhi_scaler/notice.py` decides, once per session window, at 30 minutes before its start, whether it is go or no-go by the rule above, composes a notice that gives the window, the verdict and, per pool, its instances in service, the number named and its mode, from fixed words only, and publishes it to the class-day topic its settings name, publishing nothing when they name none; it records the windows it has sent a notice for in the scaler's memory and never sends a second one for a window.
- [ ] A no-go is also published to CloudWatch through the publisher of issue E4-34, and `services/scaler/rhi_scaler/metrics.py` sends to the Prometheus workspace, for each window, its verdict at 30 minutes before its start, 1 for go and 0 for no-go; `tests/unit/scaler/test_notice.py` and `tests/unit/scaler/test_session_ready.py` cover the two.
- [ ] `src/rhi/pools/class_day.py` creates, in the pools stack that `src/rhi/pools/stack.py` builds, the class-day topic, encrypted with the AWS-managed key of SNS and refusing unencrypted transport, with an e-mail subscription to the admin's address and a text-message subscription to the instructor's phone number, both from the configuration; `tests/synth/test_class_day.py` checks it.
- [ ] `src/rhi/pools/scaler.py` lets the scaler's function publish to that topic and adds the topic's ARN to the function's settings, and `services/scaler/rhi_scaler/settings.py` reads it, no topic when the settings give none.
- [ ] Every cdk-nag finding `src/rhi/pools/class_day.py` or `src/rhi/pools/scaler.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pools_class_day.toml` or `tests/policy/acknowledged/pools_scaler.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_notice.py` passes: one notice per window, at 30 minutes before its start, go with every named pool at its number of instances in service, no-go with one instance of two still starting, a no-go handed to the CloudWatch publisher, and a second run in the same minute sends nothing.
- [ ] `uv run --locked pytest tests/unit/scaler/test_session_ready.py` passes: a window whose named pools were all in service at 30 minutes before its start reads 1, and one with an instance still starting reads 0.
- [ ] `uv run --locked pytest tests/synth/test_class_day.py tests/synth/test_scaler.py tests/unit/scaler/test_settings.py` passes: the topic has an e-mail and a text-message subscription whose endpoints are the example configuration's, the scaler's role may publish to that topic and to no other, the function's settings name it, and settings without it read as no topic.
- [ ] The owner sets up text messages in the account so that the class-day topic can reach the instructor's phone, and confirms the topic's e-mail subscription (needs aws:console).
- [ ] Before a real or a rehearsed session, the instructor receives the notice by e-mail and on their phone (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.4, 7.3, 10
