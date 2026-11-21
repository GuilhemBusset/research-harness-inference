+++
id = "E4-40"
title = "The session switch: one record the admin CLI turns on and off, off when it is absent"
labels = ["size:M", "kind:app"]
epic = 6
+++

## Context

The endpoint serves students during class sessions only, and the instructor decides when a session
is on: there is no schedule in configuration (section 5.6; ADR 0003, decision 1). One record, the
session switch, says on or off. The gateway reads it and the pipeline's deployment hold reads it
(later issues); this issue gives the record its shape and the admin the commands that write it.

**Where it lives.** In the pool-state table, beside the pools' modes, as an item no pool can be:
partition key `_session`, sort key `switch`. A pool's name is lower-case letters, digits and
hyphens (`services/common/rhi_common/pool_config.py`), so no pool can take that key. The item holds
whether the switch is on, who set it and when. No deployment writes it, and a missing item reads as
off: the endpoint serves no student until the instructor first switches a session on.

**Who.** The admin CLI acts with the admin's own credentials. It records as who set the switch the
name of the caller's identity, the last part of the identity's ARN as AWS's caller-identity call
returns it, never the whole ARN, which carries the account.

**Where the table is.** The admin CLI finds each table of the deployed stage by its logical ID
(`admin/rhi_admin/main.py`, `table_name`). The pool-state table is in the pools stack; its name is
found the same way, by its logical ID in that stack. The repository holds no table name.

The pools' own modes and the commands that change them are issue #191's; this issue adds none.

## Deliverables

- [ ] `services/common/rhi_common/records.py` defines the record of the session switch: on or off, who set it and when, read from and written to the pool-state item with partition key _session and sort key switch, and a missing item reads as off.
- [ ] `services/common/rhi_common/tables.py` names that item's two keys beside the pool-state table's other records.
- [ ] `admin/rhi_admin/session.py` switches the session on and off, recording who and when, and reads the switch for the status, with the caller's credentials.
- [ ] `admin/rhi_admin/main.py` offers the commands session on, session off and session status, and finds the pool-state table in the pools stack by its logical ID; the status prints on or off, who set it and when.
- [ ] `tests/unit/common/test_records.py` and `tests/unit/admin/test_session.py` cover the record and the commands against stubbed clients.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_records.py` passes: the record writes and reads back on and off with who and when, a missing item reads as off, and an item with another value for on is refused.
- [ ] `uv run --locked pytest tests/unit/admin/test_session.py` passes: session on then session status prints on with who and when, session off then session status prints off, a second session on leaves one item, and who is the last part of the caller's ARN with no account in it.
- [ ] `tests/unit/admin/test_session.py::test_the_switch_cannot_be_a_pool` finds the switch's partition key refused as a pool name by the pool configuration's parser.
- [ ] In the account, the admin switches a session on and off with the admin CLI, and session status reads each change back (needs aws:account).

## References

- `docs/design/system-design.md`, sections 5.6 and 11.3
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 1
- `services/common/rhi_common/records.py`
- `admin/rhi_admin/main.py`
