+++
id = "E4-40"
title = "The session switch's key stays one no pool can take"
labels = ["size:S", "kind:test"]
epic = 6
+++

## Context

The endpoint serves students during class sessions only, and the instructor decides when a session
is on with one record, the session switch (section 5.6; ADR 0003, decision 1). The record is built:
`SessionSwitch`, `switched` and `session_is_on` in `services/common/rhi_common/records.py`, its item
`SESSION_SWITCH_KEY` in `services/common/rhi_common/tables.py`, the scaler's reads, and the admin
CLI's commands session on, session off and session status (`admin/rhi_admin/session.py`), which the
class-day runbook shows. The gateway's refusal (issue E4-41) and the deployment hold (issue E4-42)
read the same record.

What is not yet held by a test is the rule the record's place rests on. The switch is an item of the
pool-state table under the partition key `_session`, beside the pools' own items, which sit under
the pool's name. A pool's name is lower-case letters, digits and hyphens (the pool configuration's
parser, `services/common/rhi_common/pool_config.py`), so no pool can be named `_session` today. A
later change that widened the names a pool may take could let a pool's records and the switch share
one partition, and nothing would fail. This issue adds the test that would.

Walking the commands through on the deployed system is already an obligation of the class-day
runbook's issue (#215); this issue adds none.

## Deliverables

- [ ] `tests/unit/common/test_pool_config.py` has a test that the pool configuration's parser refuses a pool named by the switch's partition key, read from `SESSION_PARTITION` of the records module rather than written out.
- [ ] The docstring of `services/common/rhi_common/pool_config.py` says, where it gives the rule for a pool's name, that the rule keeps the session switch's partition key free.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py tests/unit/common/test_records.py tests/unit/common/test_tables.py tests/unit/admin/test_session.py` passes.
- [ ] `tests/unit/common/test_pool_config.py::test_no_pool_can_take_the_session_switch_s_key` passes: a pool configuration that names a pool by the records module's SESSION_PARTITION is refused for that pool's name.

## References

- `docs/design/system-design.md`, sections 5.6 and 11.3
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 1
- `services/common/rhi_common/records.py`
- `admin/rhi_admin/session.py`
