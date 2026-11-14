+++
id = "E4-25"
title = "A scheduled workflow that opens an issue when the pinned engine has a serious advisory"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = "E4"
blocked_by = ["E0-17", "E0-23", "E3-03"]
+++

## Context

Patching (section 9): a scheduled GitHub Actions workflow, with no AWS access, checks the pinned
engine version against published advisories. When the version falls in the range of a high or
critical one, it opens an issue; it is the only workflow allowed to write issues. The issue is a
plain one for the owner: the advisory's public identifier, the pinned version and the fixed version.
Turning it into backlog work is the owner's step, through the backlog tooling. The fix redeploys
and refreshes warm instances. Automated digest updates wait a cooling-off period before adoption, 7
days by default and none for a security fix (section 17): a compromised release is usually caught
within hours (evidence E16). vLLM published 98 security advisories in 2026 (evidence E23).

The check itself is a script with its tests, which a workflow runs once a day. The workflow holds
no AWS credential and no secret of the repository: it uses the token GitHub gives the run, limited
to reading the contents and writing issues. Advisories are public, so the issue it opens names
nothing that is not. The advisories are those of GitHub's global security advisory database for
the pip package vllm, read through GitHub's REST API with the run's token, and their severity is the
database's. The script uses the standard library only, so it needs no pin of its own.

The workflows' YAML is read by the test of CI's workflow, `tests/unit/test_ci_workflow.py`, with the
parser that issue E0-28 pins; this issue extends that test rather than pinning a parser of its own,
and comes after issue E0-17, the other change to a workflow file, so that the two never wait for the
owner at the same time.

This issue creates `.github/workflows/advisories.yml` and changes `.github/dependabot.yml`, protected
paths on the safety surface (ADR 0001, decision 10); the builder may change them because this issue
names them. The factory's GitHub identity cannot push a change to a workflow file (ADR 0001,
amendment 2), so the engine hands this issue's certified change to the owner, who pushes it and
merges its pull request (`docs/factory/green-light.md`, step 7; issue E0-23).

## Deliverables

- [ ] `tools/advisories/check.py` reads the engine version of the version file of issue E3-03, reads the advisories of GitHub's advisory database for the pip package vllm, and exits non-zero naming the advisory's identifier when the version is inside the affected range of one rated high or critical; it imports nothing outside the standard library.
- [ ] With the option that opens issues, it opens one plain issue per such advisory, which gives the advisory's public identifier, the pinned version and the fixed version and nothing else from the advisory, and opens none while an open issue already names that identifier.
- [ ] `.github/workflows/advisories.yml` runs it once a day and on manual dispatch, with no AWS credential, no secret other than the run's own token, and permissions limited to reading the contents and writing issues.
- [ ] No other workflow of the repository has the permission to write issues, and `tests/unit/test_ci_workflow.py` checks the workflows' permissions and the cooling-off periods of `.github/dependabot.yml`.
- [ ] `.github/dependabot.yml` watches the image digests under the services folder with a cooling-off period of 7 days, like its other entries, which security updates do not wait for.
- [ ] `tools/advisories/tests/` covers the comparison on recorded advisories and the opening of issues against a stub of GitHub, and `pyproject.toml` adds it to the test paths.

## Acceptance criteria

- [ ] `uv run --locked pytest tools/advisories/tests` passes: a version inside a critical advisory's range fails and opens one issue that gives the identifier, the pinned version and the fixed version, a second run opens none, a version above the fixed version passes, and a moderate advisory neither fails nor opens an issue.
- [ ] `tests/unit/test_ci_workflow.py::test_only_the_advisory_workflow_writes_issues` fails when `.github/workflows/advisories.yml` asks for a permission beyond reading the contents and writing issues, names a secret or an AWS credential, or when another workflow file asks to write issues.
- [ ] `tests/unit/test_ci_workflow.py::test_updates_wait_a_week` fails when an entry of `.github/dependabot.yml` has a cooling-off period under seven days or none.

## References

- `docs/design/system-design.md`, sections 9, 17
- `docs/design/evidence.md`, E16, E23
- `docs/adr/0001-software-factory.md`, amendment 2
- `docs/factory/green-light.md`
