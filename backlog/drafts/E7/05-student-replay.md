+++
id = "E7-05"
title = "Schedule independent student conversations and class openings"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-02", "E7-03", "E7-04"]
+++

## Context

The sweep and the background for live trials share a scheduler. A seat is a student identity, including both conversations in the several-agents shape.

## Deliverables

- [ ] `tools/evaluate/replay.py` drives the frozen plan on the student gateway, mapping slots to canary identities and maintaining independent conversations with causal tool waits, replacements and bounded drain.
- [ ] It supports reserved live seats, seeded openings within one minute, separate warm-up prefixes or identities, per-repetition cache separation, and stops traffic on cancellation or deadline.
- [ ] It records eligible versus actual dispatch, started/completed/abandoned counts, fan-out, planned arrivals and observed coverage with distinct warm-up, opening and measured traffic.
- [ ] `tools/evaluate/tests/test_replay.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_replay.py` shows that one slow seat cannot delay another seat’s next conversation and two agents of one seat use the same identity while distinct seats have distinct identities.
- [ ] `tools/evaluate/tests/test_replay.py` exercises both shapes, one reserved seat, a missed start, p95 lag above 250 ms, partial traffic and a hard drain cutoff on a fake clock.
- [ ] `tools/evaluate/tests/test_replay.py` verifies paired prefix isolation and refuses insufficient keys or unverified fresh-prefix conditions before claiming a valid cell.

## References

- `docs/design/agent-session-evaluation.md`, Workload and measurement protocol
- `spikes/dry_run_class.py`
