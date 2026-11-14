+++
id = "E1-06"
title = "Lint every synthesized template with cfn-lint"
labels = ["size:S", "kind:test"]
epic = "E1"
blocked_by = ["E1-04"]
+++

## Context

Section 8.3 lists cfn-lint among the checks of a pull request. This issue pins it and runs it as a
test. That puts it in every gate at once: `scripts/check.sh tests` runs the repository's tests on a
laptop and in CI's job tests, which is part of the required check, and the implement loop's verifier
runs the locked tests on every head. No separate step of the gate script or of a workflow is needed.

AGENTS.md asks that every new tool be recorded in the installation inventory of `setup/README.md`.

## Deliverables

- [ ] `pyproject.toml` and `uv.lock` pin cfn-lint to an exact version in the dev group.
- [ ] `setup/README.md` lists cfn-lint among the Python tools of its installation inventory.
- [ ] `tests/synth/test_cfn_lint.py` runs cfn-lint over every template the app synthesizes, and over every CloudFormation template kept in the setup folder.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/synth/test_cfn_lint.py` passes on the app as it stands.
- [ ] `tests/synth/test_cfn_lint.py::test_reports_an_error` fails a template, built in the test, that has a resource of an unknown type.

## References

- `docs/design/system-design.md`, section 8.3
- `AGENTS.md`, sections "Toolchain" and "Validating a change"
