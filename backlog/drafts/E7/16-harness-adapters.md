+++
id = "E7-16"
title = "Run pinned student harnesses on their normal gateway routes"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-14", "E7-15"]
+++

## Context

Reuse S3’s client command adapters and the student profiles. A client must progress to its next tool action from its actual previous response.

## Deliverables

- [ ] `tools/evaluate/adapters.py` runs Claude Code, Codex, OpenCode, Pi and the SDK tool loop in the trial workers with their pinned student-facing profiles and only the trial forwarder as inference destination.
- [ ] Adapters support the tasks assigned to each harness including same-conversation follow-ups, local research tools, Claude Code worker-overlap evidence and structured trial completion or failure.
- [ ] `tools/evaluate/tests/test_adapters.py` and `tests/container/test_evaluate_adapters.py` cover the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_adapters.py` checks the five client commands and profiles using fake transports, covers all three wire formats and verifies follow-up context, child evidence and failed tool handling.
- [ ] `tests/container/test_evaluate_adapters.py` drives the pinned clients headlessly through scripted local tool exchanges in the trial environment, without a cloud account (needs host:container-runtime)

## References

- `docs/design/agent-session-evaluation.md`, Course quality and the grader
- `spikes/s3_harness_task.py`
