+++
id = "E0-05"
title = "A pilot that measures the examiner on seeded defects in CDK changes"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = "E0"
blocked_by = ["E1-07", "E1-08", "E1-09", "E2-46", "E2-49"]
+++

## Context

The examiner's certificate is the last check before a change to IAM or to the network is merged and
deployed. Its catch rate on infrastructure code has never been measured (ADR 0001, decision 2). The
ADR asks for a pilot on seeded defects before those certificates are trusted, and reopens the choice
of examiner if the pilot shows it adds nothing (alternative 4).

The pilot is a corpus of small defective changes and a runner. Running it costs one examination per
defect, with both vendors logged in, so the run is the owner's; agents build and test the corpus and
the runner.

The tree keeps changing after this issue, so the corpus is pinned: every patch is written against
one commit of `main`, which the manifest records, and the runner applies it in a scratch worktree of
that commit. No test of the repository applies a patch to the current tree, so later issues never
break the corpus. A defect may change a file or add to it: a statement added to a role, an ingress
rule added to a group. The four classes match safety invariants the examiner already names in
`tools/implementation-loop/loop.toml`. Two classes need resources the pinned commit must already
hold: a security group to open to the internet (issue E2-46) and a table whose retention or deletion
protection to remove (issue E2-49), which is why the pilot waits for both.

## Deliverables

- [ ] `tools/examiner-pilot/defects/` holds at least twelve defects as patch files against the CDK app, three in each of four classes: an IAM policy broader than the change needs, a resource exposed to the internet, a retention or a deletion protection removed, a gate or a test weakened.
- [ ] `tools/examiner-pilot/defects/manifest.toml` records the commit of `main` the patches are written against and, for each defect, its class, the one file it changes, the sentence that describes the defect, and whether a test of the repository already fails on it.
- [ ] `tools/examiner-pilot/run.py` applies each patch in a scratch worktree of the recorded commit, asks the examiner role for its findings through the engine's own agent interface, and records whether a finding names the seeded defect; it never commits, pushes or merges, and it refuses to run when a patch does not apply to the recorded commit.
- [ ] `tools/examiner-pilot/README.md` says how the owner launches the pilot and what a run costs, and `pyproject.toml` adds `tools/examiner-pilot/tests/` to the test paths.

## Acceptance criteria

- [ ] `uv run --locked pytest tools/examiner-pilot/tests` passes: the manifest names twelve defects or more, three or more per class, each patch is a unified diff that changes only the file its entry names, and the runner computes the catch rate per class from a fake examiner's answers.
- [ ] `uv run --locked pytest tools/examiner-pilot/tests` shows, in a git repository built in the test, that the runner applies a patch at the recorded commit, refuses a patch that does not apply, and leaves no commit, branch or push behind.
- [ ] The pilot is run with the Codex examiner, its catch rate per class is reported to the owner, and the owner decides whether the examiner's certificates on IAM and networking are trusted (needs human:owner-decision).

## References

- `docs/adr/0001-software-factory.md`, decision 2, Consequences and alternative 4
- `tools/implementation-loop/loop.toml`
