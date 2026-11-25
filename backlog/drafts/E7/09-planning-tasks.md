+++
id = "E7-09"
title = "Grade three optimization model and LaTeX planning tasks"
labels = ["size:S", "kind:test"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

Use the S3 bicycle workshop task as the course exemplar and keep trusted references outside the student workspace.

## Deliverables

- [ ] `tools/evaluate/course/optimization/` supplies three immutable, versioned student instances, task inputs, reference solutions and independent grading over feasibility, integrality, capacity, objective and table agreement.
- [ ] The grader consumes produced numerical artifacts with stated tolerances and accepts equivalent optimal solutions.
- [ ] `tools/evaluate/tests/test_optimization.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_optimization.py` accepts all three references, rejects a copied exemplar on changed demand or capacity, and rejects a feasible plan with a wrong optimum or disagreeing LaTeX table.

## References

- `docs/design/agent-session-evaluation.md`, Course quality and the grader
- `spikes/s3_task/check_task.py`
- `spikes/s3_reference/model.py`
