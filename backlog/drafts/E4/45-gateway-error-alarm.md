+++
id = "E4-45"
title = "Alarm on the gateway's own error rate, for when Grafana is down"
labels = ["size:M", "kind:infra"]
epic = 6
blocked_by = [219]
+++

## Context

Alerts that must work when Grafana is down are CloudWatch alarms to SNS, and the gateway's error
rate is one of them (section 7.3). The gateway's objective is that 99.5% of requests are not a 5xx
of the gateway's own fault; the alarm is coarser and faster: more than 5% of requests failing for
the gateway's own fault over 5 minutes, by default (section 17).

**The gateway's own faults.** Every error the gateway means to give names a cause of section 6.2,
and each 5xx cause names a fault elsewhere: a pool starting or out of capacity, a pool disabled or
scheduled-only, no session on, the engine, the time limit. A fault of the gateway itself is logged
with the cause `none` and answered 500 (`services/gateway/rhi_gateway/telemetry.py`). The other half
is a 5xx that the public load balancer answers itself because no gateway task answered: without it,
a gateway that is down would log nothing and never alarm.

**How the share is computed**, so that builder and examiner read it the same way: the count of the
gateway's log records whose status is 500 or more and whose cause is `none`, made by a metric filter
on the gateway's log group, plus the public load balancer's HTTPCode_ELB_5XX_Count, over the public
load balancer's RequestCount, which counts the requests it answered itself too. The metric filter
writes to the namespace rhi/gateway with no dimension at all, so no student dimension can appear
(section 6.6).

**Configuration.** The share and the period are the keys gateway_error_alarm_percent, 5 by default,
and gateway_error_alarm_minutes, 5 by default, optional so that a configuration in SSM written before
this issue loads unchanged; the CDK app reads them when it synthesizes the alarm.

**Where it lives.** In the guardrails stack, which reads the gateway's log group and the public load
balancer from their stacks and is read by none; the alarm acts on the alerts topic, whose subscriber
is the admin's address (issue #219).

## Deliverables

- [ ] `src/rhi/guardrails/alarms_gateway.py` counts, with a metric filter on the gateway's log group, the records whose status is 500 or more and whose cause is none, in a metric of the namespace rhi/gateway with no dimension.
- [ ] It raises an alarm, acting on the alerts topic, when those records plus the public load balancer's own 5xx answers exceed gateway_error_alarm_percent of the load balancer's requests over gateway_error_alarm_minutes, and `src/rhi/guardrails/stack.py` adds it to the guardrails stack without creating a resource of its own.
- [ ] `src/rhi/config.py` gains gateway_error_alarm_percent and gateway_error_alarm_minutes, 5 and 5 by default; `config.example.toml` gives them; `tests/unit/test_config.py` holds the defaults.
- [ ] Every cdk-nag finding `src/rhi/guardrails/alarms_gateway.py` brings is fixed or acknowledged with its reason in `tests/policy/acknowledged/guardrails_alarms_gateway.toml`, and `tests/synth/test_alarms_gateway.py` checks the filter and the alarm.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_alarms_gateway.py tests/unit/test_config.py` passes: under the example configuration the alarm divides the gateway's own 5xx records and the load balancer's own 5xx by the load balancer's requests and compares the share with 5% over 5 minutes, the metric filter matches status 500 or more with cause none and has no dimension, the alarm's action is the alerts topic, and a configuration that leaves the two keys out loads 5 and 5.
- [ ] In the account, outside a session, the gateway's service held at zero tasks for 5 minutes while requests are sent to the endpoint raises the alarm to the alerts topic's subscriber, and the alarm clears once the service is back (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 6.2, 6.6, 7.3 and 17
- `services/gateway/rhi_gateway/telemetry.py`
