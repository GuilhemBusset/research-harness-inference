+++
id = "E0-29"
title = "Engine: give a failed deployment's fix-forward the reporter's facts, from commit statuses only"
labels = ["size:S", "kind:factory", "loop:infra"]
epic = "E0"
blocked_by = ["E0-07"]
+++

## Context

A red deployment status alone does not tell the factory what to fix. Section 8.4 adds a structured
report to a failure, as further commit statuses on the same commit, one per fact, each assembled
from allow-listed parts (issue E0-11): the stage and action that failed, the logical IDs of failed
resources with CloudFormation's status codes, the IDs of failed smoke tests, the last lifecycle
stage an instance reached. Each such status's context is the stage's deployment or verification
context followed by a slash and a suffix. Amendment 4 of ADR 0001 makes commit statuses the whole
report: no commit comment.

Issue E0-07 sends a failed deployment down the broken-main path with feedback that names only the
failed context and its state. This issue builds the fix-forward's summary from the reporter's facts.
The engine reads those statuses and nothing else about the deployment, so that no text from AWS can
reach an agent through it: the reporter has already reduced every fact to names and enumerations.

Measurement statuses (issue E0-14) live under another prefix, `measure/`, and are not part of a
deployment's summary.

This issue changes the engine, a protected path on the safety surface (ADR 0001, decision 10); the
builder may change the files named below because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/github.py` gains a read of every commit status of a commit, with its context, state and description, and the in-memory GitHub of the tests gains the same.
- [ ] `tools/implementation-loop/implement_loop/engine.py` gives the fix-forward of a failed deployment a summary built only from the commit statuses of the merge commit whose context is a configured context, or that context followed by a slash and more: their contexts, states and descriptions, and nothing from a commit comment or any other text about the deployment.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_deploy_summary.py` covers it.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_deploy_summary.py` passing and no earlier test removed or weakened.
- [ ] `tools/implementation-loop/tests/test_deploy_summary.py::test_the_summary_comes_from_statuses_only` gives a fix-forward a summary that names a failed resource and a failed smoke test from the fake GitHub's statuses, and nothing from a commit comment the fake also holds.
- [ ] `tools/implementation-loop/tests/test_deploy_summary.py::test_other_contexts_are_left_out` shows that a status under the measure/ prefix, and one of a context that is not configured, are not in the summary.

## References

- `docs/adr/0001-software-factory.md`, decision 5 and amendment 4
- `docs/design/system-design.md`, section 8.4
