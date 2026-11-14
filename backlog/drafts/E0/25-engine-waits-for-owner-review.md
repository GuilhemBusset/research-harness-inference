+++
id = "E0-25"
title = "Engine: wait for the owner's review when GitHub refuses a merge for want of one"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

Publish this issue only with issue E0-15, that is, only if the owner answers yes to decision 8 of
the design; with a no, delete both drafts.

With a yes, a pull request that touches a protected path needs the owner's approving review before
GitHub merges it (section 8.4, control 5; ADR 0001, amendment 3). The amendment says the engine
already has an outcome for such an issue. It does not: the land step counts a refused merge as a
merge refusal, sends the issue back to verification, and after three refusals makes it wait as if
GitHub were failing (`WAIT_GITHUB` in `implement_loop/engine.py`). That costs verification rounds,
and the waiting issue says the wrong thing to the owner.

This issue gives that case its own wait. The head is certified and `required` is green on it: what
is missing is a person, so nothing is verified again, and the landing claim is released while the
owner reviews.

**How the engine knows.** Not from the text of GitHub's refusal, which is not a contract: from the
pull request's review decision, the field reviewDecision that GitHub's GraphQL API gives for a pull
request (APPROVED, CHANGES_REQUESTED or REVIEW_REQUIRED), read with `gh api graphql`. A refused merge
while that decision is REVIEW_REQUIRED or CHANGES_REQUESTED is a wait for the owner; once it is
APPROVED, the merge is tried again.

This issue changes the engine's state machine, a protected path (ADR 0001, decision 10); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/github.py` reads a pull request's review decision, the field reviewDecision of GitHub's GraphQL API, and the in-memory GitHub of the tests gains the same.
- [ ] `tools/implementation-loop/implement_loop/engine.py` treats a refused merge whose pull request's review decision is REVIEW_REQUIRED or CHANGES_REQUESTED as a wait for the owner, with a new kind owner_review, defined in `tools/implementation-loop/implement_loop/model.py`, instead of counting a merge refusal.
- [ ] While it waits for that review the engine verifies nothing again, holds no landing claim, and has said on the pull request, once, that it waits for the owner's review.
- [ ] Resumed, the engine merges the same certified head once the review decision is APPROVED, and keeps waiting while it is not; a pull request whose head moved goes back to verification as before.
- [ ] Every other refused merge is handled as before.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_owner_review.py` covers it.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_owner_review.py` showing that a fake GitHub that refuses a merge while the review decision is REVIEW_REQUIRED leaves the issue waiting owner_review after one refusal, with no further verification and the landing claim released, and with no earlier test removed or weakened.
- [ ] `tools/implementation-loop/tests/test_owner_review.py::test_a_given_review_merges_the_certified_head` shows that once the fake's review decision is APPROVED, a resumed run merges the same head and the issue ends done.
- [ ] `tools/implementation-loop/tests/test_owner_review.py::test_other_refusals_are_unchanged` shows that a refusal while the review decision is APPROVED, or empty, still counts as a merge refusal.

## References

- `docs/adr/0001-software-factory.md`, decision 10, amendments 2 and 3
- `docs/design/system-design.md`, sections 8.4, 15
