+++
id = "E6-26"
title = "Runbooks and repository texts: capacity, a scaler that does not run, upgrades"
labels = ["size:M", "kind:docs"]
epic = "E6"
blocked_by = ["E6-20", "E6-21", "E6-24", "E6-25"]
+++

## Context

The system now runs on sessions. E6-20 rewrote the runbooks of a session and of suspended launches,
and E6-19 the student guide. The other documents still describe a pool that wakes on a request, a
hold, a warm pool, an idle time, a nightly stop and an instance refresh. This issue brings them to
the design (system design, section 9, and the sections each document cites):

- `docs/runbooks/capacity-failure.md` becomes the runbook of a session short of capacity (section
  5.4): how it shows (the alarm of E6-25, the causes `warming` and `capacity_unavailable`, the five
  numbers and the cause of `session status`); that an instance asked for stays asked for, so the
  first act is to wait; what each cause calls for (`insufficient-capacity`, `quota`,
  `launches-suspended`, `launch-failed`); starting the class on the instances that have arrived;
  and the students' own subscriptions when none has.
- `docs/runbooks/scaler-not-running.md` is new: the runbook of a scaler that does not run (section
  5.1). A scaler that does not run leaves every pool as it is, and a session's instances then stay
  past its end. It says how that shows (the alarm on the scaler), how to empty a group by hand
  (lift the scale-in protection of its instances, then set its desired capacity to zero), where to
  read why the scaler fails, and how to see that it runs again.
- `docs/runbooks/image-and-engine-upgrade.md` says that a deployment replaces no instance: a
  release reaches the instances of the next session at their launch, the verification books an
  instance to check it, and the canary start of an image promotion is the one GPU instance that
  runs outside a session (section 4.3).
- `docs/runbooks/owner-setup.md` lists the three new configuration keys, `session_max_hours`,
  `capacity_alarm_minutes` and `check_wait_minutes`, no longer lists the retired ones, and gives
  the recurring task as booking each session ahead of its class and stopping it after (section
  8.6).

Each runbook keeps the four sections `tests/unit/test_runbooks.py` asks for (when, check, act,
verify), and shows only commands the CLIs accept. A step that needs the account is written as the
command the owner runs: no account, region, resource name or console link appears.

`README.md` and the description in `pyproject.toml` call the project a scale-to-zero endpoint;
`scripts/pipeline/README.md` describes the canary's wake and the refresh after an image promotion.
`pyproject.toml` is a file many issues touch: this issue changes its description and nothing else.

After this issue nothing deployed changes.

## Deliverables

- [ ] `docs/runbooks/capacity-failure.md` is the runbook of a session short of capacity, and covers what Context lists for it.
- [ ] `docs/runbooks/scaler-not-running.md` is the runbook of a scaler that does not run, covers what Context lists for it, and is in the list of `tests/unit/test_runbooks.py`.
- [ ] `docs/runbooks/image-and-engine-upgrade.md` describes a release that reaches instances at their launch, with no instance refresh to wait for.
- [ ] `docs/runbooks/owner-setup.md` lists the three new configuration keys with their defaults, lists none of the retired ones, and gives booking each session as the recurring task.
- [ ] No file under `docs/runbooks/` describes a wake by a request, a hold, a warm pool, an idle time, a nightly stop, a pool mode or a session switch as something the system has.
- [ ] `README.md`, the description in `pyproject.toml` and `scripts/pipeline/README.md` describe an endpoint the instructor provisions by session, a canary that books its instance, and an image promotion with no refresh after it.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_runbooks.py tests/unit/test_docs.py` passes: the new runbook has its four sections, and every command the runbooks show parses.
- [ ] `git grep -n -e scale-to-zero -e idle_minutes -e hold_seconds -e warm_instances -e nightly_stop -- README.md pyproject.toml docs/runbooks scripts/pipeline/README.md` prints nothing.
- [ ] The runbooks of a session short of capacity and of a scaler that does not run are each walked through once on the deployed system, and the date is recorded at the foot of each (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.3, 5.1, 5.4, 8.6 and 9
- `docs/runbooks/capacity-failure.md`
- `docs/runbooks/image-and-engine-upgrade.md`
- `docs/runbooks/owner-setup.md`
- `tests/unit/test_runbooks.py`
