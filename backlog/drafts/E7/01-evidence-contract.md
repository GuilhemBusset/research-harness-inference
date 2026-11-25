+++
id = "E7-01"
title = "Define the versioned evaluation manifest and evidence records"
labels = ["size:M", "kind:test"]
epic = "E7"
+++

## Context

Other issues use this public format to exchange observations and reproduce a scorecard offline. The local execution journal will hold private booking and delivery state separately.

## Deliverables

- [ ] `tools/evaluate/contracts.py` defines a versioned public manifest with distinct workload, treatment and run identity, required cell and trial identities, declared limits, requested settings, observed provenance and explicit missing values.
- [ ] The same module validates typed request, trial, host sample and cell records using enumerated fields, relative times, ordinals, finite numeric values and public digests, with stable serialization and content hashes.
- [ ] The suite contract includes stable task/instance IDs, fixture and grader digests, applicable harness features, and grade/invalid outcomes for the task authors.
- [ ] `pyproject.toml`, `ty.toml` and `ruff.toml` include the new evaluation tool in the locked lint, type and test checks, including default pytest discovery of `tools/evaluate/tests/`.
- [ ] `tools/evaluate/tests/test_contracts.py` covers the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] Round trips in `tools/evaluate/tests/test_contracts.py` preserve known records, their units and hashes across independent reads and reject unknown schema versions, unexpected fields, private identifiers and nonfinite measurements.
- [ ] `tools/evaluate/tests/test_contracts.py` distinguishes missing usage from zero, a completed failure from an invalid attempt, and two treatments of the same planned workload.

## References

- `docs/design/agent-session-evaluation.md`, Reproducing and comparing runs
