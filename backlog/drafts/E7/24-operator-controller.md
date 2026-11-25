+++
id = "E7-24"
title = "Run the complete operator evaluation from the terminal"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-20", "E7-21", "E7-23"]
+++

## Context

The skill will submit the same typed request that this controller executes directly. Its only live target is an operator-selected, already-deployed pool.

## Deliverables

- [ ] `tools/evaluate/controller.py` binds the run journal, preflight, issue publisher, booking, measurements, reports and cleanup into one bounded operator invocation with scoped runtime lookups.
- [ ] `tools/evaluate/cli.py` and `scripts/evaluate.sh` expose run, run resume, status, cancellation, publish resume and issue-backed comparison alongside offline planning, comparison and explicit export.
- [ ] The operator sees progress, remaining time, the issue URL, both verdicts and limiting rows, report paths, pending publication, and documented exit codes for pass, measured miss and invalid/incomplete or inconclusive evidence.
- [ ] `tools/evaluate/tests/test_controller.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_controller.py` drives the terminal workflow through the normal entry point with fake external services and verifies preflight, confirmed issue, exact booking, measured cells, conditional cleanup, local report and terminal comment in order.
- [ ] `tools/evaluate/tests/test_controller.py` exercises issue-creation outage before booking, capacity refusal, worker error, lost response and publication-only retry, verifying cleanup and no duplicate inference or GitHub writes.
- [ ] `tools/evaluate/tests/test_controller.py` demonstrates restart on the same issue, preservation of completed cells and measured failures, cancellation, exit codes 0/1/2 and delivery status independent of verdict.

## References

- `docs/design/agent-session-evaluation.md`, How the operator runs it
