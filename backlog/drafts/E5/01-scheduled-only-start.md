+++
id = "E5-01"
title = "Start a scheduled-only pool by the admin's command, never by a student's request"
labels = ["size:M", "kind:app"]
epic = "E5"
blocked_by = ["E4-02", "E4-03", "E4-05"]
+++

## Context

The upgrade pool runs `wake = "scheduled-only"`: a schedule or an admin command starts it, never a
student's request (sections 4.2 and 5.1). Most of that is delivered by other issues: held requests
and explicit wakes never wake such a pool (issues E2-05 and E2-78), a request for one with nothing
in service and no capacity asked for ends at once with the cause `pool_scheduled_only`, while one
that arrives as a schedule or the admin is starting it is held without a wake (issue E2-25), and the session windows
of the schedule pre-warm it with the instances each window names (issue E4-05). What is missing is
the admin's command. The schedule is configuration and changes only with a deployment, and spike S6
(issue E5-05) or an unplanned session must start the pool without one. The admin CLI calls the
scaler (section 5.1); the scaler stays the only writer of a pool's size.

The design says an admin command starts the pool, not for how long. This issue reads it as a window
the admin opens on the pool, from now to an end time the admin gives, with a number of instances
that is 1 unless the admin names more (the default of section 17 for a session window). The scaler
treats it as a session window that has already started. After its end the pool follows the usual
rules and goes to zero once idle. Ceilings and stops win over it, as they win over the schedule
(section 5.1).

## Deliverables

- [ ] `admin/rhi_admin/pools.py` adds a command that opens a window on a pool until an end time, with a number of instances that is 1 unless given, and a command that closes it; each writes the pool's state record with who made the change and when, then asks the scaler to run now.
- [ ] The command that opens a window refuses an end time that has passed and a number of instances above the pool's max_instances, and then writes nothing.
- [ ] `services/common/rhi_common/records.py` gains the admin's window in a pool's state record: its end time, its instances, who opened it and when.
- [ ] `services/scaler/rhi_scaler/decide.py` keeps, for a pool in mode enabled or scheduled-only, at least the instances of an admin's window that has not ended, as it does inside a session window of the schedule, and never for a pool that is disabled, budget-latched or at a ceiling.
- [ ] `tests/unit/admin/test_pools.py` covers the two commands against stubbed clients, and `tests/unit/scaler/test_scheduled_only.py` covers the rule.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/scaler/test_scheduled_only.py` passes: with no request at all, a window opened until 18:00 keeps one instance of a scheduled-only pool until 18:00, the pool goes to zero idle_minutes after the window has ended, a closed window keeps nothing, and a pool that reaches its ceiling inside a window is let go as its instances fall idle.
- [ ] `tests/unit/scaler/test_scheduled_only.py::test_a_stop_wins_over_a_window` shows that an open window starts neither a disabled pool nor a budget-latched one.
- [ ] `uv run --locked pytest tests/unit/admin/test_pools.py` passes: opening and closing a window each write the expected record, with who and when, and call the scaler once; a window with more instances than max_instances, or with an end time in the past, writes nothing.

## References

- `docs/design/system-design.md`, sections 4.2, 5.1, 11.3, 17
