+++
id = "E7-22"
title = "Checkpoint evaluation cells and recover after interruption"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

The local journal holds run identity, issue number, delivery and booking intents, and cell attempts across driver restarts. Public evidence is a separate validated projection.

## Deliverables

- [ ] `tools/evaluate/journal.py` provides atomic state transitions, one local execution owner per run, a durable private journal and append-only typed observations under the ignored evaluation output directory.
- [ ] It persists identities and intents before external actions and checkpoints whole cells with workload and served fingerprints, retaining interrupted and superseded attempts linked to their replacements.
- [ ] Resume preserves measured failures and selects whole uncompleted or invalid work that fits the authorized window while restricting reuse to matching workload and serving fingerprints.
- [ ] `tools/evaluate/tests/test_journal.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_journal.py` interrupts writes at booking, issue delivery and cell completion boundaries and recovers an unambiguous run ID, pending action and intact earlier observations.
- [ ] `tools/evaluate/tests/test_journal.py` rejects concurrent execution owners and incompatible reuse, preserves completed failures in the denominator and retains earlier partial evidence after replacement.

## References

- `docs/design/agent-session-evaluation.md`, Booking and running; Reproducing and comparing runs
