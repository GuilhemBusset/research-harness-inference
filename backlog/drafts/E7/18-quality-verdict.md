+++
id = "E7-18"
title = "Compute course success rates and paired quality decisions"
labels = ["size:M", "kind:test"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

This reader scores a versioned suite with declared policy. The initial 80% absolute floor and five percentage point paired margin are proposed defaults in course-v1.

## Deliverables

- [ ] `tools/evaluate/quality.py` computes success counts and absolute floors by family, harness and load, counts instances succeeding on all repeats and reports successful task time, tasks per hour and host cost per success including failed and background work.
- [ ] It implements the design’s equal family/shape weighting and matched instance and repeat bootstrap with 10,000 deterministic draws, a 95% interval and an inconclusive outcome for missing pairs, overlapping margins or degenerate precision.
- [ ] `tools/evaluate/tests/test_quality.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_quality.py` checks unequal harness coverage, a failing research family, invalid or superseded attempts and a zero-success denominator against hand-computed values.
- [ ] `tools/evaluate/tests/test_quality.py` verifies paired treatment order, deterministic intervals, lower-bound equality at minus five points, and separate pass, fail and inconclusive cases at both loads.

## References

- `docs/design/agent-session-evaluation.md`, Reading a result
