+++
id = "E4-10"
title = "Alarms that work when Grafana is down: the gateway's error rate, a GPU-hour ceiling, a class-day no-go"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E2-29", "E2-30", "E2-90", "E4-34", "E4-36"]
+++

## Context

The other alarms of section 7.3: the gateway's error rate, a GPU-hour ceiling reached, a class-day
no-go. The budget thresholds reach the alerts topic through the budget's own notifications (issue
E4-04). A pipeline failure already reaches the owner by SNS: the reporter publishes every failed or
errored execution, with its details, to the pipeline stack's own topic, whose subscriber is the
admin's address (issue E0-22). The pipeline lives outside the stage, under a name no template of the
stage can know, so this issue adds nothing for it.

The gateway's objective is that 99.5% of requests are not a 5xx of the gateway's own fault (section
7.3); the alarm is coarser and faster: more than 5% of requests failing for the gateway's own fault
over 5 minutes, by default (section 17). The design does not list which failures are the gateway's
own. Here they are a 5xx that carries none of the causes of section 6.2, since each of those names a
fault elsewhere (the pool, AWS capacity, the engine, the time limit), and a 5xx that the public load
balancer answers itself because no gateway task answered. Without the second, a gateway that is down
would log nothing and never alarm. The load balancer forwards to the gateway through the rule of
issue E2-90, so this issue comes after it.

How the share is computed, so that builder and examiner read it the same way: the gateway's own 5xx
answers, counted by a metric filter on the gateway's log records of issue E2-29 by their status and
cause fields, plus the public load balancer's HTTPCode_ELB_5XX_Count, over the public load
balancer's RequestCount, which counts the requests it answered itself too. The metric filters write
to the namespace rhi/gateway with no dimension at all, so no student dimension can appear (section
6.6).

The share and the period are the keys gateway_error_alarm_percent, 5 by default, and
gateway_error_alarm_minutes, 5 by default (section 17), so that a configuration in SSM written before
this issue loads unchanged; the CDK app reads them when it synthesizes the alarm.

Whether a pool is at a ceiling, and a class-day no-go, come from the scaler's data points in the
namespace rhi/scaler (issue E4-34): AtGpuHourCeiling with the dimension Pool, and ClassDayNoGo,
which the class-day notice publishes (issue E4-06). An alarm on ClassDayNoGo stays without data
until the notice publishes one, which is harmless.

The alarms live in the guardrails stack, which reads the gateway's log group and the public load
balancer from their stacks; nothing reads the guardrails stack (issue E4-36).

## Deliverables

- [ ] `src/rhi/guardrails/alarms_service.py`, which `src/rhi/guardrails/stack.py` adds to the guardrails stack without creating a resource of its own, counts, with a metric filter on the gateway's log group, the requests the gateway answered with a 5xx and no cause of section 6.2, in a metric with no dimension.
- [ ] It raises an alarm when those answers plus the public load balancer's own 5xx answers exceed gateway_error_alarm_percent of the load balancer's requests over gateway_error_alarm_minutes.
- [ ] `src/rhi/config.py` gains gateway_error_alarm_percent and gateway_error_alarm_minutes, 5 and 5 by default; `config.example.toml` gives them; `tests/unit/test_config.py` holds the defaults.
- [ ] It raises, per pool, an alarm when the scaler reports the pool at a GPU-hour ceiling, and a single alarm when the scaler publishes a class-day no-go; every alarm acts on the alerts topic.
- [ ] Every cdk-nag finding `src/rhi/guardrails/alarms_service.py` brings is fixed or acknowledged with its reason, and every action its roles need on every resource is listed with its reason, in `tests/policy/acknowledged/guardrails_alarms_service.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_alarms_service.py tests/unit/test_config.py` passes: under the example configuration the error-rate alarm divides the gateway's own 5xx and the load balancer's own 5xx by the load balancer's requests and compares the share with 5% over 5 minutes, no metric filter has a dimension, the ceiling and no-go alarms read the scaler's namespace, each alarm has the alerts topic as its action, and a configuration that leaves the two keys out loads 5 and 5.
- [ ] In the account, outside a session, the gateway's service held at zero tasks for 5 minutes raises the error-rate alarm to the alerts topic's subscriber, and the alarm clears once the service is back (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 6.2, 6.6, 7.3, 17
