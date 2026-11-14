+++
id = "E0-15"
title = "Owner review for changes to the trust boundary, and for nothing else"
labels = ["size:S", "kind:factory", "loop:infra"]
epic = "E0"
blocked_by = ["E0-02", "E0-03", "E0-25"]
+++

## Context

Publish this issue only if the owner answers yes to decision 8 of the design; with a no, delete
the draft, and issue E0-25 with it.

Ordinary backlog work merges autonomously. The design recommends that changes to the trust boundary
itself wait for the owner: the pipeline stack, the bootstrap policy, `.github/`, the gates and
`tests/policy/` (section 8.4, control 5). ADR 0001's third amendment records the same
recommendation, for the protected paths and nothing else, and says it is the owner's decision.

Three things must hold first. The protected paths include the pipeline stack and the bootstrap
policies (issue E0-02). The factory pushes as its own GitHub App (issue E0-03): GitHub never lets
the author of a pull request approve it, so a pull request the factory opened with the owner's own
login could never get the owner's review. And the engine waits for that review instead of retrying
the merge (issue E0-25).

**Whose handle.** CODEOWNERS names a GitHub login, which the design does not give and the builder
must not guess. It is the owner part of the repository's address: `docs/factory/green-light.md`,
step 3, sets the clone's `origin` to https://github.com/<owner>/<repository>.git, and an issue's
worktree keeps that remote, so `git remote get-url origin` prints it in the builder's sandbox and
the examiner's. The handle is public already, in that address; it identifies no AWS account.

This issue changes `.github/CODEOWNERS`, `setup/pr-only-main.json` and `tests/policy/`, protected
paths, the first two on the safety surface (ADR 0001, decision 10); the builder may change them
because this issue names them. Applying the changed ruleset to GitHub needs an administrator, so it
is the owner's (`setup/apply-ruleset.sh`).

## Deliverables

- [ ] `.github/CODEOWNERS` assigns the repository's owner, written as @ followed by the owner part of the address that git remote get-url origin prints in the checkout, to every protected prefix and protected file of the engine's configuration module, and to no other path.
- [ ] `setup/pr-only-main.json` requires a code owner's review in its pull-request rule, and changes nothing else.
- [ ] `tests/policy/test_codeowners.py` compares `.github/CODEOWNERS` with the protected prefixes and files of the engine's configuration module, and checks that the ruleset requires a code owner's review.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/policy/test_codeowners.py` passes, and fails when a protected path has no owner entry, when an unprotected path has one, or when the ruleset stops requiring a code owner's review.
- [ ] `python3 -m json.tool setup/pr-only-main.json` exits 0.
- [ ] The owner applies the changed ruleset with `bash setup/apply-ruleset.sh` and checks that a pull request touching a protected path waits for their review (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 8.4, 15
- `docs/adr/0001-software-factory.md`, amendments 2 and 3
- `setup/README.md`
