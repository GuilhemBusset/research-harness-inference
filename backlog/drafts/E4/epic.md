+++
id = "E4"
kind = "epic"
title = "Phase 3: ceilings, schedules, every dashboard and alarm, and what the instructor needs to run a class"
labels = ["kind:infra"]
+++

Phase 3 of the rollout (`docs/design/system-design.md`, section 14): ceilings and the kill switch,
schedules and the class-day notice, all dashboards and alarms, `parked`, the runbooks of section 9,
the privacy notice and the data commands, and the student guide.

Exit criterion, from the design: a dry-run class on the coursework model, with the expected number
of concurrent harness sessions for an hour, within the objectives of section 7.3 and the ceilings.
It is an obligation on issue E4-26.

The pools are those of phase 2: the mock pool and the coursework pool (section 4.2). The upgrade
pool is phase 4 and is not built here; nothing in this epic needs it, and every rule here is written
for any pool, so that it applies to the upgrade pool when it comes.

Several issues here depend on a decision of section 15 that the owner has not made: the dates of
the sessions (decision 1), the class size, which sets each session's number of instances (decision
5), the monthly ceiling and the GPU-hour ceilings (decision 6), whether deployments in course weeks
wait for a person (decision 8), the region and who signs the privacy notice (decision 9), and
whether a managed model may stand in for a session when no GPU capacity is available (decision
12). Each issue is written for the design's default and carries the decision as an obligation. The
managed-model stand-in of decision 12 is not designed, so nothing here builds it.

Done when every sub-issue is closed.
