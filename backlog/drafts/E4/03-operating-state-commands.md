+++
id = "E4-03"
title = "One operating state per pool, changed only by the admin"
labels = ["size:M", "kind:app"]
epic = "E4"
blocked_by = ["E2-08", "E2-12", "E2-56"]
+++

## Context

The four modes (`enabled`, `scheduled-only`, `disabled`, `budget-latched`) live in one record per
pool that the scaler and the gateway read. It survives deployments, and only the admin CLI clears a
latch or re-enables a pool (section 11.3). A kill switch that a schedule or a deployment can undo
does not hold; one record that only a person clears does (section 5.1). Disabling a pool is the
admin's kill switch; the budget latch of issue E4-04 is the automatic one.

After three abandoned launches in a row the scaler suspends a pool's launches and waits for the
admin (sections 5.1 and 5.4; issue E2-08): clearing that mark is also the admin's. The mark is the
attribute launch_suspended of the pool's memory record in the pool-state table (issues E2-05 and
E2-67); the command clears that attribute and leaves the rest of the scaler's memory as it was.

The admin CLI uses the admin's own credentials and finds each table in the deployed stack by its
logical ID (issue E2-56). It finds the scaler function the same way, by the logical ID issue E2-12
gives it, to ask for a run; the repository holds no function name.

`parked` is something else: a deployment flag that removes infrastructure (section 11.2; issue
E4-17). It is not a mode, and no command here sets it.

## Deliverables

- [ ] `admin/rhi_admin/pools.py` adds commands that show every pool's state, disable a pool, re-enable it to its configured mode, clear a budget latch, clear the mark launch_suspended of issue E2-08 in the pool's memory record of issue E2-67, and ask the scaler to run now; each change writes who made it and when.
- [ ] `admin/rhi_admin/main.py` offers those commands and finds the scaler function in the deployed stack by its logical ID, with the caller's credentials.
- [ ] No deployment writes the record: a pool without a record is in its configured mode, and a deployment never resets a latch or re-enables a disabled pool.
- [ ] `tests/unit/admin/test_pools.py` covers the commands against stubbed clients, and `tests/synth/test_tables.py` gains a check that no template writes an item of the pool-state table.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/admin/test_pools.py` passes: disabling a pool, re-enabling it and clearing a latch each give the expected record with who and when, clearing a latch that is not set changes nothing, asking for a run invokes the function found by its logical ID, and no command writes the mode parked.
- [ ] `tests/synth/test_tables.py::test_no_deployment_writes_pool_state` fails when a template writes an item of the pool-state table.

## References

- `docs/design/system-design.md`, sections 5.1, 5.4, 6.4, 11.2, 11.3
