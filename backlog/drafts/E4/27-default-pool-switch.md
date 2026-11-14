+++
id = "E4-27"
title = "One command that switches the default model to a pool that is up"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-16", "E2-27", "E2-73", "E4-03"]
+++

## Context

On a class day with no capacity for a pool, the runbook has one fallback command: switch the default
model to a pool that is up (section 5.4). The default pool is where `claude-*` model names go
(section 6.2, step 4), so the switch moves every Claude Code session without a change on a student's
machine. `GET /v1/models` says which pool is the default (issue E2-27), and `POST /wake` maps a model
name the way the body filter does, so both follow the switch.

The switch is a record beside the pools' state, written by the admin CLI with who and when, like the
modes (section 11.3; issue E4-03). It survives deployments; the configured default applies when no
switch is recorded. It is kept in the pool-state table, which the gateway already reads and only the
admin writes, under a key that no pool's name can take (a pool's name holds only lower-case letters,
digits and hyphens, issue E2-04), so that a reader of a pool's state never takes it for a pool.

The gateway reads it on the cycle on which it already reads every pool's state every 5 seconds
(issue E2-73), so no request causes a read, and a change reaches every task within 60 seconds.

## Deliverables

- [ ] `services/common/rhi_common/records.py` defines the default-pool switch: the pool, who set it and when, stored under a key no pool's name can take; `tests/unit/common/test_records.py` covers it.
- [ ] `admin/rhi_admin/pools.py` adds a command that sets the default pool, refusing a pool that is not configured or has nothing in service, and one that returns to the configured default; `admin/rhi_admin/main.py` offers both.
- [ ] `services/gateway/rhi_gateway/readiness.py` reads the switch on the cycle on which it reads the pools' states, and keeps the last value read.
- [ ] `services/gateway/rhi_gateway/body.py` maps claude- model names to the recorded default, falls back to the configured default when none is recorded, and `services/gateway/rhi_gateway/local.py` names the same default in `GET /v1/models`.
- [ ] `tests/unit/admin/test_pools.py`, `tests/unit/gateway/test_readiness.py`, `tests/unit/gateway/test_body.py` and `tests/unit/gateway/test_local.py` cover the switch.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/admin/test_pools.py tests/unit/gateway/test_readiness.py tests/unit/gateway/test_body.py tests/unit/gateway/test_local.py` passes: after the command, a claude- model name is sent to the new default within 60 seconds on a clock the test controls and the models route names it, a pool with nothing in service is refused, and returning to the configured default undoes the switch.
- [ ] `uv run --locked pytest tests/unit/common/test_records.py` passes: the switch converts to and from a DynamoDB item, and its key is refused as a pool's name.

## References

- `docs/design/system-design.md`, sections 5.4, 6.2, 11.3
