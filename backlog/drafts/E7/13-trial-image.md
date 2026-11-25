+++
id = "E7-13"
title = "Build a pinned course image for the five student harnesses"
labels = ["size:M", "kind:app"]
epic = "E7"
blocked_by = ["E7-01"]
+++

## Context

Use the existing harness and SDK pins in spikes/harnesses.py and the root uv lock. Record transitive harness installations so an operator can rebuild the environment from the repository.

## Deliverables

- [ ] `tools/evaluate/image/` defines a digest-pinned Linux trial image, its locked harness installations, solver and SDK tools, with a local inventory of versions, dependency locks and the image identity.
- [ ] `setup/README.md` documents how the operator prepares and verifies the image and where the dependencies are installed.
- [ ] `tools/evaluate/tests/test_image.py` and `tests/container/test_evaluate_image.py` cover the observable behavior in the acceptance criteria.

## Acceptance criteria

- [ ] `tools/evaluate/tests/test_image.py` verifies the image recipe and transitive lock use the repository’s pinned harness versions and detects an incompatible inventory.
- [ ] `tests/container/test_evaluate_image.py` starts the prepared image, verifies the executable versions and runs a small offline HiGHS solve without cloud access (needs host:container-runtime)

## References

- `docs/design/agent-session-evaluation.md`, Course quality and the grader; Implementation and proof
- `spikes/harnesses.py`
- `pyproject.toml`
