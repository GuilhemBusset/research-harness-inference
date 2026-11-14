+++
id = "E2-41"
title = "A pool's instance role, with a minimal Session Manager policy"
labels = ["size:M", "kind:infra"]
epic = "E2"
blocked_by = ["E2-49", "E2-57", "E2-71"]
+++

## Context

Instances have no public IP and no SSH; debugging is through Session Manager with a custom minimal
policy, because the AWS-managed policy would let every instance read all SSM parameters, including
this project's configuration (section 4.3; evidence E23). The role can read its model prefix, read
its pool's secret, write metrics and logs, pull its image, and complete lifecycle actions for its
own group.

Each grant is made by the issue that creates its resource, once both exist. This issue creates the
role and grants what already exists: the instance's heartbeat rows in the heartbeat table (issue
E2-49), remote write to the workspace (issue E2-57), the pool's own log group (created here), and the
lifecycle actions of the pool's group (issue E2-71). The log group has the fixed name
`/rhi/pools/<pool>`, so that Grafana's role names it without a reference to this stack (issue E2-60). The secret is issue E2-43's, the release
parameter, the release's files and the engine image are issue E2-42's, and the model prefix is issue
E3-07's; each of those issues grants this role what it needs.

The session actions are the five Session Manager needs and no more: `ssm:UpdateInstanceInformation`,
`ssmmessages:CreateControlChannel`, `ssmmessages:CreateDataChannel`,
`ssmmessages:OpenControlChannel` and `ssmmessages:OpenDataChannel`. AWS grants them on every resource
only, so they are listed with their reason in the module's acknowledgement file.

This issue also creates `tests/policy/test_instance_role.py`. `tests/policy/` is a protected path
(ADR 0001, decision 10): the builder may write the files named below because this issue names them.
The invariant holds from now on, before the parameter and the secret exist: an instance role may read
no parameter and no secret other than its own pool's.

## Deliverables

- [ ] `src/rhi/pools/role.py` gives each pool's instances a role that may: write the heartbeat rows of its own pool in the heartbeat table; write metrics to the workspace; write logs to its pool's log group; and complete and record heartbeats for the lifecycle actions of its own group.
- [ ] Each pool has a log group of its own for its instances, named /rhi/pools/<pool>, and `src/rhi/pools/group.py` gives the pool's launch template this role.
- [ ] Session Manager access comes from a policy written here with the five session actions of Context only; the AWS-managed instance policy is not attached.
- [ ] Every cdk-nag finding `src/rhi/pools/role.py` or `src/rhi/pools/group.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pools_role.toml` or `tests/policy/acknowledged/pools_group.toml`.
- [ ] `tests/synth/test_pool_role.py` checks the role of the mock pool, and `tests/policy/test_instance_role.py` fails when an instance role may read a parameter or a secret other than its own pool's, may read the configuration's parameters, or carries the AWS-managed instance policy.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_role.py tests/policy/test_instance_role.py` passes on the app as it stands: the mock pool's role writes only its pool's heartbeat rows and its own log group /rhi/pools/mock, and holds the five session actions and no other action on every resource.
- [ ] `tests/policy/test_instance_role.py::test_detects_the_managed_policy` fails a template, built in the test, whose instance role carries the AWS-managed Session Manager policy, and `tests/policy/test_instance_role.py::test_detects_another_pools_secret` fails one whose instance role may read another pool's secret.

## References

- `docs/design/system-design.md`, section 4.3
- `docs/design/evidence.md`, E23
