+++
id = "E4-09"
title = "Alarms that work when Grafana is down: the fleet, the scaler, and time in service outside sessions"
labels = ["size:M", "kind:infra"]
epic = "E4"
blocked_by = ["E2-72", "E4-34", "E4-36"]
+++

## Context

Alerts that must work when Grafana is down are CloudWatch alarms to SNS (section 7.3). Three of them
guard money from outside the scaler, so that a broken scaler cannot cost money (section 5.1): a
pool that has wanted capacity for 10 minutes with nothing in service (also section 5.4); instances
running in a group for 20 minutes with nothing in service; a scaler that has not completed a run for
five minutes. The first two therefore read Auto Scaling's own metrics, never the scaler's; the third
reads the data point the scaler publishes after each completed run (issue E4-34), and a missing
point is what it alarms on.

Two more come from the scaler's own rules. After three abandoned launches the scaler suspends a
pool's launches, alarms and waits for the admin (section 5.1). And an alarm fires when a pool is in
service long outside sessions (section 11.3): 3 hours by default (section 17), from the minutes the
scaler publishes (issue E4-34).

How each reads, so that builder and examiner read the same metrics:

- Wanted with nothing in service: the group's GroupDesiredCapacity above 0 and its
  GroupInServiceInstances at 0, in each of 10 one-minute periods.
- Running with nothing in service: GroupTotalInstances plus WarmPoolPendingCapacity above 0 (an
  instance being prepared for the warm pool runs before it is stopped, section 5.1), and
  GroupInServiceInstances at 0, in each of 20 one-minute periods.
- The scaler: the metrics of issue E4-34, in the namespace rhi/scaler with the dimension Pool
  where they have one: RunCompleted missing for 5 one-minute periods, LaunchesSuspended at 1, and
  MinutesInServiceOutsideSessions above the threshold.

The threshold is the key outside_sessions_alarm_hours, 3 by default (section 17), so that a
configuration in SSM written before this issue loads unchanged; the CDK app reads it when it
synthesizes the alarm. The alarms live in the guardrails stack (issue E4-36), which reads the pools'
groups from the pools stack; nothing in the pools stack reads them. Every alarm publishes to the
alerts topic of issue E4-36.

## Deliverables

- [ ] `src/rhi/pools/group.py` turns on the group metrics of every pool's group, the warm pool's included.
- [ ] `src/rhi/guardrails/alarms_fleet.py`, which `src/rhi/guardrails/stack.py` adds to the guardrails stack without creating a resource of its own, raises, per pool, from Auto Scaling's metrics as the context reads them, an alarm when the pool has been wanted for 10 minutes with nothing in service, and one when the group has had instances running for 20 minutes with none in service.
- [ ] It raises an alarm when the scaler has published no completed run for 5 minutes, treating missing data as breaching, and, per pool, one when its launches are suspended and one when it has been in service outside sessions longer than outside_sessions_alarm_hours; every alarm acts on the alerts topic, and none of their metrics has a dimension that identifies a student.
- [ ] `src/rhi/config.py` gains outside_sessions_alarm_hours, 3 by default; `config.example.toml` gives it; `tests/unit/test_config.py` holds the default.
- [ ] Every cdk-nag finding `src/rhi/pools/group.py` or `src/rhi/guardrails/alarms_fleet.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pools_group.toml` or `tests/policy/acknowledged/guardrails_alarms_fleet.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_alarms_fleet.py tests/unit/test_config.py` passes: under the example configuration the periods are 10, 20 and 5 minutes as stated, the first two read Auto Scaling's namespace, the scaler's alarm breaches on missing data, the time outside sessions is compared with 180 minutes, every alarm has the alerts topic as its action, and a configuration that leaves outside_sessions_alarm_hours out loads 3.
- [ ] In the account, the scaler's schedule disabled for six minutes raises its alarm, and enabling it again clears the alarm (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.1, 5.4, 7.3, 11.3, 17
