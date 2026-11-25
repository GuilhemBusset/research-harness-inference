+++
id = "E7-12"
title = "Grade research answers against a frozen local source packet"
labels = ["size:M", "kind:test"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

Search, PDF reading and citation verification must run offline with public, redistributable inputs. Self-authored fixtures are sufficient.

## Deliverables

- [ ] `tools/evaluate/course/research/` supplies three versioned research instances, a frozen source packet including a PDF, local search tool, references and independent graders.
- [ ] The grader verifies the required fact, unit, source identifier and cited page or table against the packet for the evidence table and conclusion.
- [ ] `tools/evaluate/tests/test_research.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_research.py` runs without network, verifies all reference submissions and rejects a plausible wrong source location, a wrong unit and an answer copied to a changed instance.

## References

- `docs/design/agent-session-evaluation.md`, Course quality and the grader
