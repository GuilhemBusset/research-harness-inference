+++
id = "E6-01"
title = "A session record per pool, with its bounds"
labels = ["size:M", "kind:app"]
epic = "E6"
+++

## Context

The endpoint serves during sessions the instructor books (system design, section 5.6). A session is
a booking of one pool: a number of instances, a start and an end. It is one item of the pool-state
table per pool, beside the scaler's note and memory. Later issues of this epic make it replace the
session switch and the pool modes.

This issue adds the record, the rules every reader and writer shares, and the one configuration key
the rules need. The admin CLI, the scaler, the gateway, the deployment hold, the verification, the
canary and the spikes will all call the same functions, so a record written by any of them, or by
hand, is read under the same bounds. The functions are pure: they call nothing and take the time as
an argument, as `rhi_common.records` does.

Nothing reads the record yet. After this issue the deployed system does what it did before.

A time is a whole number of seconds since the epoch. `set_by` is who last wrote the record and
`set_at` when. A session is booked from the moment it is written, and active from `starts_at` to
its end. A pool has one session at a time.

## Deliverables

- [ ] `services/common/rhi_common/records.py` has the record Session, with the pool, `instances`, `starts_at`, `ends_at`, `set_by` and `set_at`, stored in the pool-state table under the pool's name and the sort key `session`, which `services/common/rhi_common/tables.py` names among the table's records.
- [ ] `services/common/rhi_common/sessions.py` has `is_active` and `requested`: a session is active from `starts_at` to the earlier of `ends_at` and `session_max_hours` after the later of `starts_at` and `set_at`, and `requested` is its `instances` capped at the pool's `max_instances` while it is active, and 0 otherwise and for a pool with no session.
- [ ] The same module has `started`, which returns the session to write for a booking and refuses, with a SessionError, a pool whose session is booked or active, a number of instances outside 1 to `max_instances`, a start before now, and an end that is not after the start or is more than `session_max_hours` after it.
- [ ] It has `changed`, which returns the session with another number of instances, start or end, and `stopped`, which returns it with its end at now, or nothing for a pool with no session booked or active; `changed` refuses a pool with no session booked or active, a start moved once it has passed, a number of instances outside 1 to `max_instances`, and an end that is not after both now and the start or is more than `session_max_hours` after the later of the two.
- [ ] `src/rhi/config.py` reads the key `session_max_hours`, a whole number of at least 1 that is 12 when left out, and `config.example.toml` holds it with a comment that says what it bounds.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_sessions.py` passes: a session is active at its `starts_at` and not at its `ends_at`, one whose `ends_at` is 20 hours after its `starts_at` and its `set_at` ends 12 hours after the later of the two, and `requested` never exceeds `max_instances`.
- [ ] `tests/unit/common/test_sessions.py::test_each_refusal` shows every refusal of `started` and of `changed` that the Deliverables list, and that `stopped` returns nothing for a pool with no session booked or active.
- [ ] `uv run --locked pytest tests/unit/common/test_records.py tests/unit/common/test_tables.py tests/unit/test_config.py` passes: a Session converts to an item and back, an item that is not one is refused with a RecordError, and `session_max_hours` reads 12 when left out.

## References

- `docs/design/system-design.md`, sections 5.6 and 17
- `services/common/rhi_common/records.py`
- `src/rhi/config.py`
