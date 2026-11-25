+++
id = "E7-11"
title = "Grade parallel analysis, its figure and the reported result"
labels = ["size:M", "kind:test"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

The several-agents shape includes genuine overlap. The S3 figure exercise supplies the starting point.

## Deliverables

- [ ] `tools/evaluate/course/analysis/` supplies three paired-data tasks, reference artifacts and an independent grader for both computed series, plotted data and axes, and the requested quantitative comparison.
- [ ] The grader checks bounded worker lifecycle evidence for overlapping independent workers when the prompt requires delegation.
- [ ] `tools/evaluate/tests/test_analysis.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_analysis.py` accepts all references within the stated tolerances and rejects serial worker intervals, a swapped data series and a figure whose reported comparison conflicts with the input.

## References

- `docs/design/agent-session-evaluation.md`, Course quality and the grader
- `spikes/s3_second_session.py`
