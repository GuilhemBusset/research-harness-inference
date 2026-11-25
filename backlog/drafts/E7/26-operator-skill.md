+++
id = "E7-26"
title = "Ship the evaluate skill and verify an operator-launched screen"
labels = ["size:M", "kind:docs"]
epic = "E7"
blocked_by = ["E7-25"]
+++

## Context

The skill is the operator’s entry point from Codex or Claude Code. This is an explicit on-demand experiment outside CI/CD. Fixture tests can run in the normal gate.

## Deliverables

- [ ] `.agents/skills/evaluate/` and `.claude/skills/evaluate/` hold the paired thin skill which submits typed requests to the operator driver and presents the experiment issue and status.
- [ ] `scripts/check.sh` checks the new skill pair alongside the existing skill-pair checks.
- [ ] `docs/runbooks/agent-session-evaluation.md` documents driver and image setup, scoped prerequisites, examples for screen, batching qualification and an existing booking, cancellation, resume, offline comparison, public export and publication recovery.
- [ ] `tools/evaluate/tests/test_cli_examples.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `scripts/check.sh skills` verifies the new pair and `tools/evaluate/tests/test_cli_examples.py` validates the documented invocation forms offline.
- [ ] On a deployed pool the operator invokes the screen via the skill, observes issue creation before booking, both workload shapes and harness task results, then verifies owned-session release and the safe terminal scorecard on that same issue (needs gpu:capacity)
- [ ] The operator compares two measured configurations on the same experiment, verifies that the paired result identifies its limiting rows or missing evidence, and reproduces the comparison from the exported bundle in a fresh checkout offline (needs gpu:capacity)

## References

- `docs/design/agent-session-evaluation.md`, How the operator runs it; Implementation and proof
- `docs/runbooks/sessions.md`
