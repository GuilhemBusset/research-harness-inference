+++
id = "E4-24"
title = "Runbooks: park and unpark, image and engine upgrade, course-end teardown and data deletion"
labels = ["size:M", "kind:docs"]
epic = "E4"
blocked_by = ["E3-02", "E4-19", "E4-23", "E4-38"]
+++

## Context

The last three of the nine runbooks of section 9. Each runbook is tested once.

- **Park and unpark.** `parked` removes the load balancers, NAT, Fargate services and warm pool, and
  keeps data and the pipeline (section 11.2; issues E4-17 and E4-38). It is a configuration change, so it goes
  through the pipeline, outside the hold around sessions.
- **Machine image and engine upgrade.** A machine image goes through the promotion job, which
  promotes it only after a canary start passes (section 4.3; issue E3-02). An engine upgrade raises
  the pinned digest; the scheduled advisory check opens an issue when the pinned version has a high
  or critical advisory (section 9; issue E4-25). Either change refreshes the pool's instances, at one
  GPU start per warm instance, and the pipeline's verification asserts the served release (section
  4.3).
- **Course end.** Keys expire at course end (section 6.4); export and erase one student's data with
  the admin CLI (issue E4-19); rows, logs and series are gone by course end plus 90 days (section
  6.6); then park or delete the stage.

Each step is one of the three kinds issue E4-22 defines, and `tests/unit/test_runbooks.py` checks
them the same way. Here: parking and unparking are a change of the flag parked in the owner's
configuration file followed by the configuration step of the owner-setup script, then a deployment
through the pipeline (issues E4-17 and E4-38); starting the image promotion job is a step in the
console, or an aws command written out with placeholders (issue E3-02); raising the engine's pinned
digest is a change through the backlog and a pull request, a git step; exporting and erasing a
student's data are admin commands (issue E4-19). Deleting the stage is a step the owner takes with
their own credentials, written out in full; no agent runs it.

## Deliverables

- [ ] `docs/runbooks/park-and-unpark.md`: when to park, what parking removes and keeps, the change that parks and the one that unparks, and how to check a key still works.
- [ ] `docs/runbooks/image-and-engine-upgrade.md`: how to promote a machine image, how to raise the engine's pinned digest after an advisory, what the refresh costs, and how to check the served release.
- [ ] `docs/runbooks/course-end.md`: revoking every key, exporting what the owner keeps, erasing each student's data, parking or deleting the stage, and what retention removes afterwards.
- [ ] Each runbook has the sections when, check, act and verify, writes each step as one of the three kinds of issue E4-22, and is listed in `tests/unit/test_runbooks.py`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_runbooks.py` passes for the three runbooks: each has the four sections, each rhi and admin command it shows parses, and each script it names exists.
- [ ] Each of the three runbooks is walked through once on the deployed system, and its date is recorded at its foot (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 4.3, 6.4, 6.6, 9, 11.2
