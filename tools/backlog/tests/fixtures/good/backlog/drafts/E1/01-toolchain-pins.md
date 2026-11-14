+++
id = "E1-01"
title = "Pin the CDK toolchain"
labels = ["size:S", "kind:infra"]
epic = "E1"
+++

## Context

Nothing can be synthesized, locally or in CI, until the library and the CLI are pinned to
exact versions.

## Deliverables

- [ ] `pyproject.toml` and `uv.lock` pin the CDK library and its constructs package to exact versions.
- [ ] `cdk.json` names the app entry point and turns context lookups off.

## Acceptance criteria

- [ ] `uv sync --locked` succeeds on a machine with no AWS credentials.
- [ ] `tests/unit/test_toolchain.py` fails when a CDK pin in `pyproject.toml` is loosened to a range.

## References

- `docs/design/system-design.md`, section 8.1
