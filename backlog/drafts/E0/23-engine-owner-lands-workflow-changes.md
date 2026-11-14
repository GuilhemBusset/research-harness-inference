+++
id = "E0-23"
title = "Engine: hand a certified change to a workflow file to the owner instead of pushing it"
labels = ["size:L", "kind:factory", "loop:infra"]
epic = "E0"
blocked_by = ["E0-07"]
+++

## Context

The factory's GitHub identity has no workflows permission (ADR 0001, amendment 2; system design,
section 8.4, control 3), so GitHub refuses any push whose commits change a file under
`.github/workflows/`. That is the point: the factory cannot change its own gates. The green-light
checklist therefore says that an issue which names a workflow file is landed by the owner
(`docs/factory/green-light.md`, step 7). Two drafts change one: E0-17 and E4-25.

The engine has no path for that today. It pushes through `scripts/ship.sh` at the land step; a
refused push that is not a conflict raises `GitError` in `implement_loop/workspace.py`, and the
engine files it as an engine fault. This issue gives such an issue an outcome instead: the engine
builds, verifies and certifies the change as usual, pushes nothing, and waits for the owner to push
the branch. Then it opens the pull request itself, so that the owner is not its author and can
review it, and it leaves the merge to the owner: whether GitHub would let the App merge a change to
a workflow file is not something the engine relies on. Once the pull request is merged at the
certified head, the issue continues from `merged` as any other, through the deploying phase and
the deployment verdict of issue E0-07.

**When `main` moves while the owner has not acted.** The issue holds no landing claim while it
waits, so other issues land. Two cases follow, and the engine handles both without the owner:

- The certified head still merges into `main` without a conflict. Nothing is certified again,
  however far `main` has moved: the owner may push and merge it as it is, and what lands is checked
  like any merge, by the post-merge verification and the deployment verdict; a failure there takes
  the broken-main path. Re-certifying on every move of `main` would spend an examination each time
  another issue lands.
- The certified head no longer merges into `main` without a conflict (GitHub's mergeable state of
  the pull request once it is open, a merge the engine tries in its worktree before that). The
  engine merges `main` into the certified head in the issue's worktree, the builder resolves the
  conflict as in an ordinary round, the verifier and the examiner check the new head, and the
  engine records it as the certified head and posts it, with the command that pushes it. The new
  head descends from the old one, so the owner's push fast-forwards the remote branch, the open
  pull request follows it, and nothing is force-pushed.

The engine never updates the remote branch itself, and its comment asks the owner not to use
GitHub's button that brings a branch up to date: the head it makes is not certified.

The rule holds whatever identity the engine runs as, including the owner's own login: the engine
never pushes a workflow change itself.

This issue changes the engine's state machine, a protected path (ADR 0001, decision 10); the builder
may change the files named below because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/engine.py`, at the land step and before any push, finds whether the commits it would publish change a path under the folder .github/workflows/; if they do, it pushes nothing, releases the landing claim, and the issue waits with a new kind owner_landing, defined in `tools/implementation-loop/implement_loop/model.py`, its branch and certified head recorded.
- [ ] When it starts that wait, the engine posts on the issue one comment that names the branch and the certified head, gives the command that pushes that branch as it is, says that the owner merges the pull request the engine then opens, and asks the owner not to update the branch on GitHub.
- [ ] Resumed while the remote has no such branch, or has it at a head other than the certified one, the issue keeps waiting and its detail says which.
- [ ] Resumed with the remote branch at the certified head, the engine opens the pull request, with the same body and evidence as one it pushed itself, and keeps waiting; it never asks GitHub to merge it.
- [ ] Resumed while the certified head no longer merges into the current `main` without a conflict, the engine merges `main` into it in the issue's worktree, the builder resolving the conflict as in an ordinary round, verifies and has the examiner certify the new head, records it as the certified head, and posts one comment that names it and gives the command that pushes it.
- [ ] While the certified head still merges into `main` without a conflict, the engine verifies and certifies nothing again, however far `main` has moved; once that pull request is merged with the certified head as its head, the issue continues from merged: the post-merge verification, the evidence and the deployment verdict apply as for any other issue.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_owner_landing.py` covers it.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_owner_landing.py` covering a branch that changes the workflow file of CI: nothing pushed, the issue waiting owner_landing with the comment posted, and no engine fault filed; and with no earlier test removed or weakened.
- [ ] `tools/implementation-loop/tests/test_owner_landing.py::test_the_engine_opens_but_never_merges` shows that once the fake remote holds the branch at the certified head the engine opens one pull request and makes no merge call, and that after the fake merges it the issue ends done.
- [ ] `tools/implementation-loop/tests/test_owner_landing.py::test_another_head_keeps_waiting` shows that a remote branch at a head other than the certified one opens no pull request.
- [ ] `tools/implementation-loop/tests/test_owner_landing.py::test_a_conflict_with_main_is_certified_again` moves the fake `main` with a conflicting change, and shows the resumed issue merging `main`, verified and certified again, posting the new head, and ending done once the fake remote holds that head and the fake merges the pull request.
- [ ] `tools/implementation-loop/tests/test_owner_landing.py::test_a_clean_move_of_main_certifies_nothing` moves the fake `main` without a conflict and shows no new verification or examination.
- [ ] `tools/implementation-loop/tests/test_owner_landing.py::test_other_changes_land_as_before` shows that a branch changing only other files under `.github/` is pushed and merged by the engine.

## References

- `docs/adr/0001-software-factory.md`, decisions 5 and 10, amendment 2
- `docs/design/system-design.md`, section 8.4
- `docs/factory/green-light.md`
- `docs/factory/operating.md`
