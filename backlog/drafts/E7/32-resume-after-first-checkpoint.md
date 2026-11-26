+++
id = "E7-32"
title = "Persist the serving identity before a capacity cell becomes reusable"
labels = ["size:S", "kind:app"]
epic = 744
+++

## Context

In `tools/evaluate/phases.py` the cell's checkpoint (`checkpoint_cell`) marks the cell reusable before `_write_served` writes `served.json`. A driver that exits between the two leaves a journal with reusable observations and no serving identity, and the next start refuses the same workload, artifacts and serving configuration with `incompatible-resume`.

## Deliverables

- [ ] `tools/evaluate/phases.py` writes the serving identity of a qualifying cell before the checkpoint that makes the cell reusable.
- [ ] `tools/evaluate/tests/test_phases.py` holds a test that exits the driver between the serving write and the checkpoint, and another that exits right after the checkpoint.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_phases.py` shows a restart at either boundary keeps the journal's observations and lets the matching run continue instead of refusing `incompatible-resume`.
- [ ] `tools/evaluate/tests/test_phases.py` shows a restart under another workload or serving fingerprint is still refused `incompatible-resume`.
- [ ] `uv run --locked pytest tools/evaluate/tests/test_phases.py` exits 0.

## References

- `docs/design/agent-session-evaluation.md`
