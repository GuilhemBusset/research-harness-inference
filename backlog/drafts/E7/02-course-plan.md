+++
id = "E7-02"
title = "Plan a seeded course workload and bounded booking batches offline"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

Provide the first usable entry point, the plan command. Subsequent issues implement the planned work. The qualification matrix is 27 instance/harness pairs before trials and loads.

## Deliverables

- [ ] `tools/evaluate/plan.py` and `tools/evaluate/suites/course-v1.toml` expand the screen and qualify profiles with the design’s task assignments, limits, repetition seeds, cache policy, arrivals, rotated cell order, warm-up and opening phases.
- [ ] The planner deduplicates equivalent sweep cells while retaining their class-floor role and chooses whole batches with time reserved for drain, telemetry and cleanup.
- [ ] `scripts/evaluate.sh` and `tools/evaluate/cli.py` expose offline plan through locked uv and print the ordered manifest, counts of canary identities and agent slots, trial counts, scratch estimate, minimum window, host-hours, priced estimate with source and batch commands.
- [ ] `tools/evaluate/tests/test_plan.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_plan.py` obtains the same workload hash twice from the CLI with credentials absent and verifies that changing a seed changes the schedule.
- [ ] `tools/evaluate/tests/test_plan.py` counts 162 qualification trials for eight seats, collapses equivalent loads for one seat, and verifies a screen has one replay repetition and one anchor attempt per selected core task.
- [ ] `tools/evaluate/tests/test_plan.py` checks 1/4/8/16 slots plus class size in both shapes, distinct identity needs, 5/15 minute admissions, bounded drain and refusal to place a whole cell outside its window.

## References

- `docs/design/agent-session-evaluation.md`, Booking and running; Workload and measurement protocol; Course quality and the grader
