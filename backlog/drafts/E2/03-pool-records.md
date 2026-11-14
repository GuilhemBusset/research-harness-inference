+++
id = "E2-03"
title = "The rows the gateway, the instances and the scaler exchange"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-02"]
+++

## Context

The scaler decides from rows that other components write (section 5.1). Each gateway task rewrites a
heartbeat row every 5 seconds with its requests in flight, its requests held and the time of the
last request received, per pool; rows older than 20 seconds are ignored. Each instance's lifecycle
agent writes a heartbeat row every 5 seconds with the engine's running and waiting counts and the
release it serves: the engine image digest and the model revision (sections 4.1 and 4.3). One record
per pool holds its operating state, which the scaler and the gateway both read and which survives
deployments (section 11.3). The 5 and the 20 seconds are the defaults of section 17.

An explicit wake counts as a held request until the pool is ready, and as a request received at that
moment (section 5.1). The rule that takes a pool to zero counts from the last request received,
which is why the row carries its time: the counts of one instant cannot tell a scaler that runs
once a minute.

The gateway also needs two facts only the scaler has, to say whether a pool is `warming`, `cold` or
`unavailable` for lack of capacity (section 5.3): whether the scaler has asked for capacity, and
when a launch last found none. The scaler writes them in a note per pool, apart from the pool's
mode, which only the admin changes. The note is the one record per pool that the scaler writes and
every gateway task reads.

Phase 2 adds to these records what keeping a student on one instance needs (section 5.5); this
issue adds none of it.

Three components in three packages read and write these rows. Their shape is defined once, in the
shared package, with no AWS call. The tables they live in are issue E2-49's, and their key schema is
collected with every other table's in issue E2-67; this issue fixes the keys of its own items so that
the two agree:

| Table | Partition key | Sort key | Items |
| --- | --- | --- | --- |
| pool-state | `pool` (string) | `record` (string) | `mode` (the admin's record), `note` (the scaler's note), `memory` (the scaler's memory of earlier runs, issue E2-10) |
| heartbeats | `pool` (string) | `source` (string) | `gateway#<task ID>`, one per gateway task and pool; `instance#<instance ID>` |

Times are whole seconds since the epoch, stored as numbers. A heartbeat also carries `expires_at`,
the table's expiry attribute, one hour after it was written: readers ignore it after 20 seconds
anyway, and the expiry only clears the rows of tasks and instances that are gone. The design gives
no figure for that hour. The pool-state items carry no expiry: they must survive.

## Deliverables

- [ ] `services/common/rhi_common/records.py` defines the gateway heartbeat (task, pool, requests in flight, requests held, the time the last request was received, written at), the instance heartbeat (instance, pool, running, waiting, the engine image digest and the model revision served, the last lifecycle stage reached, written at), the pool state (mode, set by, set at) and the scaler's note (pool, the capacity the scaler asked for, the time the last launch found no capacity, written at).
- [ ] The pool's mode is one of enabled, scheduled-only, disabled and budget-latched; any other value is refused when a record is read.
- [ ] Each record converts to and from a DynamoDB item keyed as the table in Context says: the pool state and the note by pool and the record mode or note, a heartbeat by pool and the source gateway# or instance# followed by the task's or the instance's ID; times are whole seconds since the epoch.
- [ ] A heartbeat's item carries expires_at, one hour after it was written, and a function says whether a heartbeat is fresh at a given time: a heartbeat older than 20 seconds is not.
- [ ] `tests/unit/common/test_records.py` covers the four records.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_records.py` passes: each record survives a round trip through its item form, a mode outside the four is refused, parked included, and a heartbeat is fresh at 20 seconds and stale after.
- [ ] `tests/unit/common/test_records.py::test_item_keys` shows the note of the pool mock keyed by pool mock and record note, and a gateway task's heartbeat keyed by its pool and gateway# followed by the task's ID, with an expires_at 3600 seconds after its written at.

## References

- `docs/design/system-design.md`, sections 4.1, 4.3, 5.1, 5.3, 11.3, 17
