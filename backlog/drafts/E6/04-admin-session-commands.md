+++
id = "E6-04"
title = "Admin CLI: start a session, change it, stop it, and see what arrived"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-01", "E6-03"]
+++

## Context

The instructor's commands (system design, section 5.6). They are the admin CLI's, run with the
instructor's own AWS credentials, and they write the pool's session item with the rules of
`rhi_common.sessions` (E6-01). The scaler already holds a pool at its session's number (E6-02) and
writes what arrived in its note (E6-03), which `session status` prints.

The CLI already has a `session` group for the session switch (`session on`, `session off`,
`session status`), which the gateway still reads until E6-17. `on` and `off` stay as they are in
this issue, and `session status` keeps its line for the switch, before the pools' lines. E6-20
removes them.

Where the commands read what they need: a pool's `max_instances` from the settings of the scaler's
function, as `rhi_admin.pools` reads the deployed pools today; the configuration's `time_zone`,
`session_max_hours` and default pool from the configuration in SSM, as the course end is read
today. A write holds only if the item still reads what the command read, as the switch's write
does. After a write the command asks the scaler for a run, so that it applies within seconds
(`rhi_admin.pools.run_scaler`); the scaler runs one invocation at a time, so a run under way
refuses a second one, and the next run is within a minute.

The CLI is not deployed: after this issue the deployed system is unchanged, and the instructor can
book a pool.

## Deliverables

- [ ] `admin/rhi_admin/session.py` has the commands start, set and stop, which write the pool's session item with the shared functions `started`, `changed` and `stopped`, only if the item still reads what the command read, and read it again otherwise; a refusal of those functions is the command's refusal, and after a write the command asks the scaler for a run, saying so and still exiting 0 when the scaler is already running.
- [ ] `admin/rhi_admin/main.py` parses `session start <instances> [--pool P] [--from T] (--until T | --for D)`, `session set [--pool P] [--instances N] [--from T] [--until T | --for D]`, `session stop [--pool P | --all]` and `session status`; a command with no pool given acts on the configuration's default pool, and the commands on and off are unchanged.
- [ ] A time is an hour and a minute, written HH:MM, read as the next time the clock of the configuration's `time_zone` shows it, or a date and time written YYYY-MM-DDTHH:MM in that zone; a length is a whole number followed by m for minutes or h for hours.
- [ ] `session status` prints the switch's line as today and then, for each deployed pool, its session's start and end with who set it and when, or that it has none, then the note's numbers requested, asked, waiting and since when, starting and in service, and its cause.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/admin/test_sessions.py` passes: start, set and stop write the item the shared functions give, a write that meets a changed item is made again from a new read, each refusal exits 1 with its reason, and a command whose run of the scaler is refused because one is under way exits 0.
- [ ] `tests/unit/admin/test_sessions.py::test_times_and_lengths` shows an HH:MM that the clock has already passed today read as tomorrow's, a full date and time read in the configured zone, and 90m and 6h read as lengths.
- [ ] `tests/unit/admin/test_sessions.py::test_status_prints_the_supply` shows the status line of a pool with 3 requested, 1 waiting and its cause, and of a pool with no session.
- [ ] In the account, `session start 1 --for 1h` on the coursework pool is followed by `session status` reading the instance waiting or starting and then in service, and `session stop` ends it (needs gpu:capacity).

## References

- `docs/design/system-design.md`, section 5.6
- `admin/rhi_admin/session.py`
- `admin/rhi_admin/pools.py`
- `services/common/rhi_common/sessions.py`, delivered by E6-01
