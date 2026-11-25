+++
id = "E7-10"
title = "Grade debugging and follow-up revisions in one conversation"
labels = ["size:S", "kind:test"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

A student first fixes an incorrect solver result and then changes a constraint. The second instruction arrives in the same conversation.

## Deliverables

- [ ] `tools/evaluate/course/revision/` supplies three versioned broken-solver instances, ordered prompts, reference repairs and independently checkable original and changed cases.
- [ ] The grader checks final artifacts against both original and revised constraints and recomputes the result from the submitted output.
- [ ] `tools/evaluate/tests/test_revision.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_revision.py` accepts each reference and rejects an original-only repair, a hard-coded exemplar on changed data and a follow-up that breaks the original case.

## References

- `docs/design/agent-session-evaluation.md`, Course quality and the grader
