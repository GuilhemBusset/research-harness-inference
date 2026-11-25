+++
id = "E6-25"
title = "Alarm: a session short of its instances for 30 minutes"
labels = ["size:M", "kind:infra"]
epic = "E6"
blocked_by = ["E6-02"]
+++

## Context

An instance that is asked for stays asked for until it arrives, and a session filled within half an
hour says nothing. The instructor is told when it is not: an alarm on the alerts topic fires when a
session has had fewer instances in service than it asks for 30 minutes, and clears, with a second
message, when the pool has all its session asks (system design, sections 5.4 and 7.3).

It is built as the scaler's other two alarms are (`src/rhi/guardrails/alarms_fleet.py`): the scaler
publishes a data point to CloudWatch at the end of every completed run, in its own namespace and
with the pool as the only dimension, and an alarm of the guardrails stack reads it. Like them it
keeps its state while the scaler publishes nothing: the scaler's own alarm says that. The data
point is `InstancesMissing`: what the pool's session requests (E6-02) less the instances in
service, never below zero, and so zero for a pool with no active session. The alarm clears when the
session is filled, and also when it ends or is lowered to what is in service.

One alarm goes: "wanted with nothing in service", which reads Auto Scaling's own metrics and fires
after 10 minutes with desired capacity above zero and nothing in service. Under sessions that is
the ordinary state of a pool whose launch takes longer than 10 minutes or waits for capacity, and
the new alarm covers a pool that stays empty. The alarm on instances running with nothing in
service, and the one on the scaler, stay.

How long a session may be short is a configuration key, `capacity_alarm_minutes`, 30 when left out
(section 17).

After this issue the deployed alarms tell the instructor, by the alerts topic's e-mail, when a
session is short for 30 minutes and when it no longer is.

## Deliverables

- [ ] `services/scaler/rhi_scaler/cloudwatch.py` publishes, for each pool on every completed run, the data point InstancesMissing: the instances the pool's session requests less those in service, never below zero.
- [ ] `src/rhi/guardrails/alarms_fleet.py` has, for each pool, an alarm that fires when InstancesMissing is above zero in each of `capacity_alarm_minutes` one-minute periods, keeps its state when a period has no data, and acts on the alerts topic both when it fires and when it clears.
- [ ] The alarm on a group with desired capacity above zero and nothing in service for 10 minutes is removed.
- [ ] `src/rhi/config.py` reads the key `capacity_alarm_minutes`, a whole number of at least 1 that is 30 when left out, and `config.example.toml` holds it.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_cloudwatch.py` passes: a pool whose session requests 3 with 1 in service publishes 2, one with more in service than requested publishes 0, and one with no session publishes 0.
- [ ] `uv run --locked pytest tests/synth/test_alarms_fleet.py` passes: each pool has the alarm, over 30 periods of one minute with the example configuration, with the alerts topic among its alarm actions and its OK actions, and no alarm reads the group's desired capacity.
- [ ] `uv run --locked pytest tests/unit/test_config.py tests/policy` passes: `capacity_alarm_minutes` reads 30 when left out.
- [ ] In the account, a session that stays short of its instances for 30 minutes sends one message to the alerts topic's address, and a second one when it is filled or ends (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 5.4, 7.3 and 17
- `services/scaler/rhi_scaler/cloudwatch.py`
- `src/rhi/guardrails/alarms_fleet.py`
- `src/rhi/config.py`
