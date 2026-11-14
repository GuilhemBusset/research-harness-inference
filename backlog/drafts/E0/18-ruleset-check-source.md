+++
id = "E0-18"
title = "An invariant that only GitHub Actions can satisfy the required check"
labels = ["size:S", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

The fourth control of the trust boundary is a ruleset on `main` with no bypass: pull request
required, required checks that only GitHub Actions can satisfy, no force-push
(`docs/design/system-design.md`, section 8.4). GitHub lets anyone with write access set a commit
status of any name, unless the ruleset names the source it expects for each required check
(`docs/design/evidence.md`, E26). The coding agents write commit statuses, so a ruleset that names
no source would let an agent mark its own head green.

The ruleset definition of the scaffolding already names GitHub Actions as the source of the check
`required`. Nothing fails if a later change drops that. This issue adds the test. It reads the
ruleset definition, not a synthesized template, and lives with the other invariants because it
guards the same boundary.

This issue creates `tests/policy/test_ruleset.py`. `tests/policy/` is a protected path (ADR 0001,
decision 10): the builder may write the file because this issue names it. The ruleset definition
itself is not changed.

## Deliverables

- [ ] `tests/policy/test_ruleset.py` reads the ruleset definition of the setup folder and fails when a required status check names no integration, or an integration other than GitHub Actions, whose application ID is 15368.
- [ ] It fails when the check named required is not among the required status checks, when the bypass list is not empty, when the enforcement is not active, and when the pull-request rule, the rule against force-pushes or the rule against deletion is missing.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/policy/test_ruleset.py` passes on `setup/pr-only-main.json` as it stands.
- [ ] `tests/policy/test_ruleset.py::test_detects_a_check_anyone_can_set` fails a ruleset, built in the test, whose required check names no integration, and one that names another integration.
- [ ] `tests/policy/test_ruleset.py::test_detects_a_bypass` fails a ruleset, built in the test, with one bypass actor.

## References

- `docs/design/system-design.md`, section 8.4
- `docs/design/evidence.md`, E26
- `setup/README.md`
