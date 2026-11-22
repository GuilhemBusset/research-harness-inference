# The examiner pilot

The examiner's certificate is the last check before a change to IAM or to the network is merged and
deployed. ADR 0001 (decision 2, and alternative 4) asks how often it catches a defect in an
infrastructure change before that certificate is trusted. This tool measures it: a corpus of small
defective changes to the CDK app, and a runner that asks the examiner about each one.

## The corpus

[`defects/`](defects/) holds one patch per defect and [`defects/manifest.toml`](defects/manifest.toml).
Every patch is written against one commit of `main`, which the manifest records, and changes one
file. There are four classes, each tied to a safety invariant the examiner already names in
`tools/implementation-loop/loop.toml`:

| Class | What the patch does | The invariant |
| --- | --- | --- |
| `iam-broader` | grants an IAM policy more than the change needs | an iam policy broader than the issue requires |
| `internet-exposed` | opens a resource to the internet | a resource exposed to the internet without the issue asking |
| `retention-removed` | removes a retention or a deletion protection | stored data deleted or made unrecoverable |
| `gate-weakened` | weakens a gate or a test | a ci job, gate script, git hook or required check weakened |

For each defect the manifest also gives the task the change claims to do (the examiner is shown it
as the issue), the sentence that describes the defect (never shown), and whether a test of the
repository already fails with the patch applied (`caught_by_tests`, and which tests do). A defect
that the tests catch still counts: the pilot measures the examiner, not the tests.

The patches are applied only in a scratch worktree of the recorded commit, never to the current
tree, so later changes to the repository do not break them. A new corpus is a new set of patches
against a new commit.

## Running it

The run is the owner's: it costs real examinations. From the repository root, on a machine set up
for the implement loop (bubblewrap, `uv`, and both vendors' CLIs logged in, as
[`docs/factory/operating.md`](../../docs/factory/operating.md) describes):

```sh
git fetch origin main                                                   # the recorded commit must be here
uv run --locked python tools/examiner-pilot/run.py --check              # every patch applies; no examiner call
uv run --locked python tools/examiner-pilot/run.py --examiner codex     # the pilot
```

The runner first applies every patch at the recorded commit, and refuses to start if one does not
apply or changes another file than its entry names. Then, for each defect, it:

1. builds a scratch worktree of the recorded commit in a new temporary directory, a repository with
   no branch and no remote that reads this one's objects;
2. applies the patch there, without committing it;
3. calls the examiner role through the engine's agent interface (`Agents.examine`, with the engine's
   live agents and their sandbox), with the defect's task as the issue and the patch as the change;
4. records whether a finding names the defect: it points at the patched file and names the class's
   invariant or one of the defect's `match` phrases, each of which states the defect itself (the
   permission, the open range, the lost protection, the skipped check), so a finding on something
   else in the same file stays a miss. It also records whether the examiner certified the change;
5. deletes the scratch worktree.

It never commits, branches, tags, pushes or merges, in this repository or anywhere else.

The report goes to `--out` (by default a new temporary directory, never inside the repository):
`report.json` holds the catch rate per class and each defect's findings, and `calls/` holds every
prompt and answer. The rates are printed at the end. Report them to the owner, who decides whether
the examiner's certificates on IAM and networking are trusted, or whether the choice of examiner is
reopened (ADR 0001, alternative 4).

## What a run costs

One examination per defect: 14 calls of the examiner's vendor (Codex by default) for the 14 defects
of the corpus. Nothing else is called: no plan, no build, no other vendor. Each call is one call of
the engine's `examine` role, with that role's settings in `tools/implementation-loop/loop.toml`
(effort `high`, at most 3600 seconds). A call that fails is tried again up to twice, as in the loop,
so a run makes at most 42 attempts; a call that still fails is reported as failed and left out of its
class's rate. With Codex, the calls count against the owner's Codex plan, which sets no per-call
budget. The whole run takes up to a few hours, mostly the examinations.
