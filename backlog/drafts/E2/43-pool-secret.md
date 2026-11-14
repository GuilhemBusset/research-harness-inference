+++
id = "E2-43"
title = "A bearer secret per pool, read at engine start"
labels = ["size:S", "kind:infra"]
epic = "E2"
blocked_by = ["E2-41"]
+++

## Context

The four inference routes require the pool's bearer secret (section 4.1). The route allow-list on
the internal load balancer is the control; the secret is a second lock. It comes from Secrets
Manager at engine start, never from user data (section 4.3).

Two roles read it: the pool's instance role (issue E2-41), which this issue grants, and the gateway's
task role, which issue E2-30 creates and grants itself. This issue grants nothing to a role it does
not create; the pools stack exposes each pool's secret for that grant. The secret's ID reaches an
instance in its pool's release parameter (issue E2-42). Rotation is not part of phase 1: a rotated
secret needs every engine and the gateway restarted, and the cdk-nag finding for a secret without
rotation is acknowledged with that reason.

## Deliverables

- [ ] `src/rhi/pools/secret.py` creates one generated secret per pool in Secrets Manager, grants the pool's instance role the right to read it, and `src/rhi/pools/stack.py` exposes each pool's secret so that another construct can be allowed to read it.
- [ ] No template, parameter, user data or environment of a build holds the secret's value.
- [ ] Every cdk-nag finding `src/rhi/pools/secret.py` or `src/rhi/pools/stack.py` brings is fixed or acknowledged with its reason, and every action their roles need on every resource is listed with its reason, in the file of the module that created the resource, `tests/policy/acknowledged/pools_secret.toml` or `tests/policy/acknowledged/pools_stack.toml`.
- [ ] `tests/synth/test_pool_secret.py` checks the secret and who may read it.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_pool_secret.py tests/policy/test_instance_role.py` passes: one secret per pool, readable by its own pool's instance role and by no other pool's, and no template holds a literal value for it.

## References

- `docs/design/system-design.md`, sections 4.1, 4.3
