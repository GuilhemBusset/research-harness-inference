+++
id = "E2-12"
title = "Deploy the scaler: one invocation at a time, every minute"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E1-16", "E2-11", "E2-49", "E2-57", "E2-71"]
+++

## Context

The scaler is a Lambda with a reserved concurrency of 1 (section 2), so two runs never overlap. A
one-minute schedule calls it; the gateway and the admin may call it. Its role is what makes it the
only writer: nothing else in the stage may set a group's capacity.

The function lives in the pools stack, beside the groups it sizes. Its code and its libraries are
packaged with `src/rhi/packaging.py` (issue E1-16), without Docker. Its settings reach it in the
environment variable `RHI_SCALER_SETTINGS`, in the shape issue E2-11's reader defines. It logs to a
log group of the pools stack with the fixed name `/rhi/scaler`, named in its logging configuration,
so that Grafana's role names it without a reference to this stack (issue E2-60).

The gateway's service is built by issue E2-30, which grants its own task role the right to invoke
this function. This issue makes the function available to the stage for that grant and grants
nothing to a role it does not create. The admin calls it with the admin's own credentials (section
6.4), from outside the stage.

Its role needs some actions that AWS grants only on every resource: Auto Scaling's
`DescribeAutoScalingGroups`, `DescribeScalingActivities` and `DescribeWarmPool`, and Elastic Load
Balancing's `DescribeTargetHealth`. Each is listed with its reason in the module's acknowledgement
file (`tests/policy/acknowledged/`, issue E1-07), as is every cdk-nag finding the function brings.
The actions that change a group (setting its capacity, its instances' protection, suspending and
resuming its processes) are limited to this stage's pool groups.

This issue creates `tests/policy/test_single_writer.py`, an invariant that section 8.4 does not list
but that section 5.1 rests on. `tests/policy/` is a protected path (ADR 0001, decision 10): the
builder may write the files named below because this issue names them.

## Deliverables

- [ ] `src/rhi/pools/scaler.py` deploys the scaler function, packaged with the packaging module of issue E1-16 and without Docker, with a reserved concurrency of 1, a schedule that invokes it every minute and the log group named /rhi/scaler in its logging configuration, and `src/rhi/pools/stack.py` adds it to the pools stack.
- [ ] The function's environment variable RHI_SCALER_SETTINGS holds every pool's entry as the stage loaded it, the name of each pool's group and the ARN of its target group, the names of the heartbeat and pool-state tables, and the workspace's remote-write address, in the shape the settings reader of issue E2-11 reads.
- [ ] Its role may set the capacity of this stage's pool groups, set their instances' scale-in protection and suspend and resume their processes; describe groups, warm pools, scaling activities and target health; read and write the heartbeat and pool-state tables; and write to the Prometheus workspace; nothing else.
- [ ] The stack exposes the function to the stage so that another construct can be allowed to invoke it, and no other role of the stage may set a group's capacity.
- [ ] Every cdk-nag finding `src/rhi/pools/scaler.py` or `src/rhi/pools/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pools_scaler.toml` or `tests/policy/acknowledged/pools_stack.toml`.
- [ ] `tests/synth/test_scaler.py` checks the function, the schedule and the role, and reads the function's settings with the settings reader of issue E2-11; `tests/policy/test_single_writer.py` fails when any role other than the scaler's may set the capacity of a group.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_scaler.py tests/policy/test_single_writer.py` passes on the app as it stands: the mock pool's group and target group are in the function's settings, the reader accepts them, and the function logs to the log group /rhi/scaler.
- [ ] `tests/policy/test_single_writer.py::test_detects_a_second_writer` fails a template, built in the test, in which another role may set a group's desired capacity.
- [ ] `scripts/check.sh synth` exits 0 with the scaler in the pools stack.
- [ ] In the account, the scaler completes a run every minute and its metrics appear in the workspace (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 2, 5.1, 5.4, 8.4
