+++
id = "E6-20"
title = "Admin CLI and runbooks: the switch and the pool modes go"
labels = ["size:M", "kind:app"]
epic = "E6"
blocked_by = ["E6-04", "E6-16", "E6-18"]
+++

## Context

Nothing reads the session switch or a pool's mode any more: the scaler since E6-16, the gateway
since E6-17 and E6-18. The admin CLI still writes both (`session on`, `session off`,
`pool disable`, `pool enable`), and two runbooks are about them. A session is now the one operating
state (system design, sections 5.6 and 11.3): a pool that should not run is a pool nobody books,
and the kill switch is `session stop`.

The commands and the runbooks change together because `tests/unit/test_runbooks.py` parses every
command a runbook shows with the CLI's own parser: a runbook cannot show a command that is gone.

What stays of the `pool` group: `pool clear-suspension` and `pool run`, and `pool status`, which
now says one thing for each pool, whether the scaler has suspended its launches, and no mode.

The two runbooks become those of section 9, each with the sections the test asks of a runbook
(when, check, act, verify), and take the names of what they are about:

- `docs/runbooks/sessions.md`, a session booked, changed and stopped, in place of
  `docs/runbooks/session-on-and-off.md`: booking ahead of a class with `session start`, reading
  `session status` while the instances arrive, adding or removing instances and moving the end
  with `session set`, stopping with `session stop`, and what students see at each moment.
- `docs/runbooks/suspended-launches.md`, a pool whose launches are suspended, in place of
  `docs/runbooks/disabled-pool.md`: how it shows (the alarm, `pool status`, the cause
  `launches-suspended` in `session status`), finding why three launches were abandoned, and
  `pool clear-suspension` once the cause is fixed.

The other runbooks link to the two old files and show their commands in places. This issue changes
those links and those commands, and nothing else of the other runbooks: E6-26 revises their text.

The deployment hold still reads the switch beside the sessions (E6-05). With no command left to
turn it on, that read is dead; E6-21 removes it with the record.

After this issue nothing deployed changes: the CLI is the instructor's, and runbooks are documents.

## Deliverables

- [ ] `admin/rhi_admin/session.py` and `admin/rhi_admin/main.py` have no command on or off, and `session status` prints no line for the switch.
- [ ] `admin/rhi_admin/pools.py` and `admin/rhi_admin/main.py` have no command disable or enable, and `pool status` prints, for each deployed pool, whether its launches are suspended, and no mode.
- [ ] `docs/runbooks/sessions.md` replaces `docs/runbooks/session-on-and-off.md` and covers what Context lists for it, with the sections when, check, act and verify.
- [ ] `docs/runbooks/suspended-launches.md` replaces `docs/runbooks/disabled-pool.md` and covers what Context lists for it, with the same four sections.
- [ ] Every other file under `docs/runbooks/` links to the two new files where it linked to the old ones, and shows no command the CLIs no longer accept; `tests/unit/test_runbooks.py` lists the two new names.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/admin tests/unit/test_runbooks.py` passes: the parser refuses `session on`, `session off`, `pool disable` and `pool enable`, and every command the runbooks show parses.
- [ ] `git grep -n -e session-on-and-off -e disabled-pool -e "rhi_admin session on" -e "rhi_admin session off" -e "rhi_admin pool disable" -e "rhi_admin pool enable" -- docs admin tests/unit/test_runbooks.py setup` prints nothing.
- [ ] `tests/unit/admin/test_pools.py::test_status_names_no_mode` shows the status of a pool whose launches are suspended and of one whose are not.

## References

- `docs/design/system-design.md`, sections 5.6, 9 and 11.3
- `admin/rhi_admin/session.py`
- `admin/rhi_admin/pools.py`
- `docs/runbooks/session-on-and-off.md`
- `docs/runbooks/disabled-pool.md`
- `tests/unit/test_runbooks.py`
