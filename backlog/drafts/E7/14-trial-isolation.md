+++
id = "E7-14"
title = "Isolate student workspaces and grade outputs in fresh workers"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-13"]
+++

## Context

Run untrusted student-generated code on a Linux driver outside the fleet. The trusted grader checks the result of executing the candidate in a separate constrained worker.

## Deliverables

- [ ] `tools/evaluate/workers.py` creates disposable resource-limited trial homes with task inputs, bounded output and access only to their own forwarder and assigned local tools.
- [ ] The worker lifecycle isolates submitted code from grader fixtures and host credentials, checks resource limits, kills the trial process tree and removes workspaces and transcripts after grading, cancellation or timeout.
- [ ] `tools/evaluate/tests/test_workers.py` and `tests/container/test_evaluate_workers.py` cover the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_workers.py` verifies resource and mount specifications and cleanup of every started worker following partial setup or cancellation.
- [ ] `tests/container/test_evaluate_workers.py` exercises trial and grading isolation, blocked external egress and disposal after cancellation on a container host (needs host:container-runtime)

## References

- `docs/design/agent-session-evaluation.md`, Course quality and the grader
