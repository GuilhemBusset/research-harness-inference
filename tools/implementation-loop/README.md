# Implement loop engine

The engine behind `/implement` (Claude Code), `$implement` (Codex) and `scripts/implement.sh`.
Give it an epic or a single issue; it works through every open sub-issue in dependency order,
without waiting for a person and without parking, until each issue is done, delivered with
typed obligations, or (rarely, and visibly) failed.

This is a vendored copy: the engine was written in another repository and is kept here with
local patches, all listed in [`UPSTREAM.md`](UPSTREAM.md). Why this repository uses it, and what
it changes: [ADR 0001](../../docs/adr/0001-software-factory.md). The engine's own design and
rationale are the upstream ADRs
[0008](https://github.com/GuilhemBusset/coding-agent-cursus/blob/main/docs/adr/0008-implement-loop.md),
[0011](https://github.com/GuilhemBusset/coding-agent-cursus/blob/main/docs/adr/0011-autonomous-implement-loop.md)
and [0012](https://github.com/GuilhemBusset/coding-agent-cursus/blob/main/docs/adr/0012-builder-examiner-loop.md);
"ADR 0008", "ADR 0011" and "ADR 0012" in this directory mean those. How to operate it here:
[`docs/factory/operating.md`](../../docs/factory/operating.md).

It is plain Python (3.11+, standard library only; its tests need 3.12) plus `git` (2.29 or
later, for `git init --object-format`) and the authenticated GitHub CLI.
Agents (Claude Code and Codex, run headless) only produce plans, code, tests, probes, findings and
decisions through the `Agents` interface; the engine owns the issue graph, git, GitHub, the
verifier, the gates and the merges.

## Use

Normally through the skill: `/implement 10` (Claude Code) or `$implement 10` (Codex). It runs
the preflight, shows the plan, asks you once to confirm that the runs merge their own PRs, starts
the supervisor in the background and reports when it ends. The same steps by hand:

```sh
scripts/implement.sh doctor [--smoke | --canary]      # toolchain, logins, repository, commit identity, sandbox, ruleset, test runner
scripts/implement.sh plan 10                           # order, waves, warnings (read-only)
scripts/implement.sh supervise 10 --operator claude --yes --detach   # run until settled (bounded)
scripts/implement.sh run 10 --operator claude --yes    # one engine run; re-running resumes
scripts/implement.sh status 10                         # progress, cost so far
scripts/implement.sh stop 10                           # stop at the next safe point
scripts/implement.sh retry 10 [21 ...]                 # a fresh ceiling for failed issues, resume waiting ones
```

`--operator` names the agent driving the run (the builder); the other one is the examiner.
`--yes` records that the user agreed the runs merge their own PRs once every gate passes.

The loop needs this repository on GitHub, with a `main` branch pushed. Until then `doctor` fails,
on its `repository` line (no `origin` remote) or on its `base branch` line (nothing pushed yet),
and `run` and `supervise` both refuse to start, before they read an issue
([`docs/factory/green-light.md`](../../docs/factory/green-light.md)).

## The doctor

`scripts/implement.sh doctor` prints one line per check. A `FAIL` stops `run` and `supervise`; a
`WARN` does not.

| Line | Fails or warns when |
| --- | --- |
| `python`, `git`, `gh auth`, `claude`, `codex`, `codex login` | FAIL: a tool is missing or not logged in. |
| `repository` | FAIL: no `origin` remote, or `gh` cannot see the repository. |
| `base branch` | FAIL: the remote has no `main` (nothing was pushed). |
| `commit identity` | FAIL: the name or address git would commit with here (author or committer) is one the hygiene scanner refuses. The engine commits as that identity, and the history scan reads it on every commit: no branch could land. The scanner itself judges, with `tools/hygiene/hygiene.toml`; the line never prints an address it refuses. |
| `git config` | FAIL: the repository's own git configuration carries a credential (a token in a remote URL, an extra header). Agents can read that file. |
| `sandbox` | FAIL: bubblewrap is missing, or the canary, run inside the real sandbox, could read a credential path, saw a credential variable, wrote outside its worktree or brought a hidden directory back. |
| `required check` | WARN: the ruleset does not require the `required` check. |
| `pytest` | WARN: the pinned test runner does not start through uv. |
| `node` | WARN: `node` is not one of the majors the CDK CLI supports (22 or 24). |

`--canary` runs only the canary and prints everything it tried and saw. `--smoke` adds one small
real call to each vendor, inside the sandbox; it runs only when no line failed.

## Live agents

Every agent call is a fresh headless process, started inside the sandbox (below):
`claude -p --output-format json --json-schema …` or `codex exec --output-schema … -o …`, with
the answer validated against the role's schema
(`implement_loop/schemas.py`), retried twice on failure, and recorded with its prompt, answer,
duration and cost under `<run dir>/calls/` and `agents.jsonl`. Prompts are in `prompts/`;
per-role effort, models and timeouts in `loop.toml`. Here the operator is `claude`: `plan` and
`build` run on Claude Fable (`claude-fable-5-2`), `explore` on a cheap Claude model, and every
examiner role on Codex with its default model (ADR 0001, decision 2). A Codex call that decides
about a protected path is escalated to `codex_escalated_model` (`gpt-6.2-astra`): a `decide` call
with a `protected_change` request among its requests, every `arbitrate` call, and an `examine`
call on a head whose change against its base adds, changes or removes a protected path (ADR 0001,
amendment 13). A model that refuses a call (`claude_refused`, `codex_refused`: a usage limit or
quota, a rate limit, a model the account may not use) is an outage charged to nothing; with a
fallback model for that vendor (`claude_fallback_model`, `codex_fallback_model`) the same attempt
is made again at once on it, and the fallback is kept for 30 minutes.

| Role | Vendor | Access |
| --- | --- | --- |
| explore, plan, triage, arbitrate | builder (explore, plan) / examiner (triage, arbitrate) | read-only tools (Claude) / read-only sandbox (Codex) |
| build | builder | edits the issue worktree, runs anything, network; Claude's commit, push, `gh`, `aws` and `cdk deploy` commands are denied |
| examine, decide | examiner | anything, network, in a disposable worktree deleted afterwards |

Both vendors' working roles have the same access: Codex runs them with `--sandbox
danger-full-access`, like Claude Code without a sandbox (ADR 0012). Every attempt is reported to
the engine's meter: an answer is charged to the issue's call ceiling, a timeout or crash to its
infrastructure budget, and an outage (the CLI could not reach its service) to neither: the issue
waits for the supervisor to see the vendor answer again.

Those per-tool rules are a convenience, not the boundary. The boundary is the sandbox: an agent
finds no AWS material and no GitHub login, in files or in its environment. Beyond it, an agent's
commits never reach the repository (the engine commits its changes itself), and only verified,
reviewed heads are merged. Codex calls start with no MCP servers.

Every agent that can write (`build`, and `examine` and `decide` in their
disposable worktree) works against a private git directory of its run's own, at
`<repo parent>/<repo name>.loop/issue-<root>/agent-git/<id>/`. For the call, the worktree's `.git`
file names that directory instead of the worktree's own one in the shared repository. The
private directory starts as a copy of the worktree's git state: every ref, HEAD, the index, a
merge in progress, `info/` and the shared and per-worktree config. It reaches the shared objects
through an alternate, its `hooks` link to the shared hooks directory, and `core.hooksPath` still
applies, so the usual hooks fire. Every git process run in the worktree uses it, whatever the
agent's CLI does to the environment. Nothing the agent does to git, whether commits, checkouts,
branches, tags or ref deletions, changes the shared repository, so nothing has to be undone, and
refs another run or a person changes during the call are never touched. When the call ends, the
original `.git` file is put back, the private directory is deleted, and its state is reported:

- `agent_refs_deleted`: refs the agent created. They are gone with the private directory.
- `agent_ref_changes_discarded`: existing refs the agent moved or deleted. They never changed in
  the repository.
- `agent_commits_absorbed`, `agent_checkout_undone`: the agent committed, or switched branches
  or detached HEAD. The worktree stays on the issue's branch at its earlier head, the agent's
  working tree is kept, and the engine commits it. Index entries the working tree cannot rebuild,
  committed or not, are carried into the worktree's index with their blobs, so the engine's
  commit includes them: ignored paths the agent staged (`git add -f`), and executable bits it
  recorded only in the index (`git update-index --chmod`, the only record of the bit under
  `core.fileMode=false`). An entry the agent's index records as the worktree's index already
  does is left alone; every other path is committed from the working tree.
- `agent_merge_dropped`: main's merge was in progress, and the agent aborted, quit or reset it
  without committing it. The engine ends the merge too, keeping the files, so the conflict goes
  back to its author (`conflict_unresolved`, `check_conflict_unresolved`).
- `issue_branch_moved_externally`: someone outside the agent moved the issue's branch during
  the call. The agent could not have, so the engine leaves it as it is.

The engine never changes the repository's config for this. If a run is killed mid-call, the
worktree still names the private directory. The next time the engine uses or removes the
worktree, it points the `.git` file back at the directory git registered for it, carries the
index entries above (this needs no HEAD, so it also works when the branch was deleted), and
deletes the private one. If carrying fails, the worktree is left on the private directory, kept
whole, and the next use tries again, so staged work is never dropped. A git command the agent
points at another checkout (`git -C /elsewhere`) finds none: inside the sandbox the main checkout
and the other worktrees do not exist, and the repository's object store is read-only.

## The sandbox

Every agent call and every check command runs in a bubblewrap sandbox (`implement_loop/sandbox.py`;
ADR 0001, decision 3). What it is, how to check it on a host and what it does not protect:
[`docs/factory/isolation.md`](../../docs/factory/isolation.md). In short:

- The root filesystem is read-only. `/home`, `/root`, `/run`, `/mnt`, `/media`, `/var/tmp` and the
  home directory are empty, `/tmp` is private. `~/.aws`, the `gh` login, `~/.ssh` and both
  vendors' session histories do not exist in there.
- Bound back in: the tools (`loop.toml [sandbox] toolchain`), read-only; the call's worktree and
  its private git directory (read-only for a read-only role); the repository's objects,
  read-only; the run's own package caches; and, for a vendor's own calls, the one file its login
  is kept in. A check command gets no login at all.
- One environment scrub for agents and checks (`sandbox.clean_env`): no `AWS_*` or other cloud
  variable, no GitHub credential, no handle to the launching session, nothing named or shaped
  like a secret, a vendor's API key only in that vendor's own calls.
- Processes are in a PID namespace of their own: a timeout or a stop kills everything a call
  started. The network is the host's.
- Nothing runs without it. When the sandbox cannot be built, the call is not started and the
  issue waits (`environment`). No setting turns it off; the tests alone install a pass-through.
- The engine's own git, which runs outside in worktrees agents wrote to, takes no hook, no
  file-system monitor and no `ext::` transport from them for the length of a run
  (`sandbox.engine_git_env`).

The four launch sites are `LiveAgents._spawn` and `LiveAgents._codex_mcp_off` (`live.py`), the
smoke and reachability calls (`environment.py`) and `_run` (`verify.py`). The allow-list is
`loop.toml [sandbox]`, on the safety surface with `sandbox.py`.

## Standing checks and history checks

Two lists in `config.py`, run by the verifier inside the sandbox, whatever an issue says:

- `STANDING_CHECKS`: `python3 tools/hygiene/scan.py`, the public-repository scan of the tree.
  It runs on the builder's working tree before the engine commits a round, on an examiner's
  probes before they are run or adopted, on every head that is verified (before the issue's own
  commands), and on a revert before it is pushed. A finding goes
  back to its author with the scanner's redacted output: a builder's round is not committed
  (`build_not_committed`), a probe is not kept and the examiner is told why. A tree without the
  scanner fails closed.
- `HISTORY_CHECKS`: `python3 tools/hygiene/scan.py --history <base>..<head>`, the same scan over
  every commit the branch adds to `main`: each file version, each commit message, each author
  and committer. A value that was committed and removed again passes the tree scan and is still
  published with the branch. It runs on every head that is verified and again immediately before
  every push (an issue's, a revert's). A finding (exit status 1) is nothing a round can repair,
  so the builder is not asked to: the branch is abandoned unpushed, the issue ends `failed` with
  the cause `history`, and its next attempt (`retry`, or a new engine version) builds on a new
  branch from `main` (`history_refused`). A history check that could not run is a failed check
  like any other, and that head is not pushed.

Both are the scanner of the tree under judgement, which is a protected path: a head that changed
it without approval fails the protected-path gate. What they publish (a fault issue, feedback) names the
place and the rule, never the line around a value.

## How an issue moves

`pending → plan → planned → build → verify → examine → land → merged → delivered | done`, with
`waiting` (resumes by itself) and `failed` (explicit non-delivery) instead of parking.

| Phase | Who | What happens |
| --- | --- | --- |
| plan | builder, then engine, then examiner | The builder reads the repo and the issue's branch and gives every ledger item a quoted reading and a proof kind (`test`, `command`, `review`, `owner` for a capability the host lacks, `deferred` to another open issue). The engine lints it (one free retry, then it repairs it and publishes why). The examiner may only object to a misreading, a classification or a dependency, each with a quote; a classification dispute goes to the arbiter. A protected path is granted when the issue's deliverables name it (that path, or a protected directory above it; naming `scripts/` does not name `scripts/check.sh`), with a published decision; any other needs a `protected_change` request. Nothing else restricts what the builder may change. |
| planned | engine | Waits until the paths its issue names don't overlap any issue being built, and no shared file is held. |
| build | builder, then engine | Before anything is committed the engine runs the standing checks on the working tree: a round they refuse is not committed and goes back with their output. The builder writes the code, its tests, the registrations and one or more commands per item (all of them run; each names exactly one item id as its criterion, and one that names no item is reported back, not kept; an item it gives commands for runs those from then on). It may change any file, tests included, except a protected path it was not granted. Never commits. Asks with a typed request (`protected_change`, `reclassify`, `depends`, `dispute`), decided by a fresh call of the other vendor (disputes: the arbiter). A branch an earlier engine locked tests on gets one commit that releases them in CI's loop guard (`locks_released`). |
| verify | engine | Runs the standing checks and the history checks, then every command and adopted probe from its own code (a command that several items name runs once, and its result counts for each): no skips, per-process pytest reports, protected paths changed only with a grant, open findings re-run. |
| examine | examiner | In a disposable copy of the exact head: verdicts on review items, every finding at once (with memory of earlier rounds), probes, and a certificate. A probe only witnesses a finding and is never committed: the finding keeps it, and the verifier writes it into its copy of each head. A finding blocks only if the engine reproduces it and it quotes the item or names a safety invariant; others become a follow-up issue. From an issue's fourth examination since its latest plan (`Caps.examiner_bound`), only a safety invariant blocks, and a head whose items are all met is certified even without the examiner's certificate. A change that weakens a test is a finding under "check or gate bypass": nothing locks a test, the examiner reviews it in the diff. The certified head is the one that lands. |
| land | engine | One at a time across runs (a run that finds the queue held waits): the history checks once more on the exact head, then push without force, PR with `Refs #N`, evidence comment, the `required` check on the exact certified head, merge with that head SHA. A red check is never run again: it goes back to the builder with the failed job's log. Main moving means verify and a delta examination again. |
| merged → delivered/done | engine | Takes the verifier's evidence of the head that landed (nothing runs again on `main`, where CI runs every test), ticks the checkboxes with evidence links, files the follow-up issue, and closes the issue, or keeps it open as delivered with its owner and deferred obligations. A failed deployment, or a ledger item without evidence, gets one fix-forward, then a revert PR. |

Budgets: a persistent per-issue call ceiling (S 20, M 30, L 45; infra ×1.5), a per-issue dollar
ceiling over what the vendors report (S $40, M $70, L $120; infra ×1.5; `config.USD_CEILINGS`), an
infrastructure budget of 10 failed attempts, and the progress rule (two rounds that change nothing
bring a replan on the branch, two more end the issue `failed`). Conflicts with `main` cost no
round. Each Claude call also carries `--max-budget-usd` from its role (`loop.toml`: explore 2,
plan 8, build 25); a call that stops there is charged and not tried again. Codex reports tokens,
not dollars, so its side is bounded by calls. The dollar figures are provisional until the first
epic's cost is read from `agents.jsonl` (ADR 0001, decision 8); `status` shows what each issue
cost. A `failed` issue (a ceiling spent, no progress, a failed deployment that could not be reverted,
a refused history) is kept in the run's `events.jsonl` (event `failed`), not filed as a loop
fault, and revived once, with fresh ceilings, by the next engine version.
`scripts/implement.sh supervise` keeps running the engine (at most 10 runs, and no deadline unless
`--max-hours` gives one), waits out outages, runs one repair run per engine exception (never on the safety surface listed
in `config.py`), and reopens deferred items once their issue lands.

## Keeping up with `main`

Other issues land while one is in flight, so the engine brings every issue branch up to date
with `main` by merging it in (never a rebase: that would rewrite verified, reviewed or published
commits), at two points:

- **Before each build round.** A clean merge is committed and recorded as
  `updated_from_main`. On a conflict the merge stays in progress for that round: the builder
  gets the conflict markers and feedback naming the files, and may change them that round.
  The engine then concludes the merge, or aborts it if markers remain (`conflict_unresolved`) and the next round
  tries again.
- **At land, before pushing.** A clean merge makes a new head, so the issue is verified and
  certified again (a delta examination) before it may land. A conflict, there or in
  `scripts/ship.sh`, is aborted and sends the issue back to build (`conflict`).

A conflict is work for the builder, never an engine fault, and costs no round: main moving is
nobody's design fault (ADR 0012). No worktree keeps an unfinished rebase or merge between steps, and when a
run starts it aborts any that an earlier failure left behind (`worktree_recovered`). The branch
keeps every commit.

A worktree whose branch ref was deleted from under it is repaired first, when the run starts:
the branch is recreated, create-only, at the newest commit of the worktree's own HEAD reflog that
shares history with `main` and contains every commit the engine recorded for the issue
(`branch_restored`). A recorded commit that a later recorded one rewrote no longer counts: when
shipping rebases an unpublished branch, the checks commit and the old reviewed head are left out
of the verified tip's history. The working tree and index are left as they are. Without such a commit the
issue starts a new round on a new branch from `main` (`branch_restore_failed`); the branch is
never pointed at `main` or at a guess.

## Several runs on one repository

Runs for different epics or issues may share a repository. Each has its own run state and
worktrees, and the shared git writes (fetches, ref updates, worktree changes, attaching an agent
call's private git directory) are serialized across threads, runs and processes by an flock on
`<git common dir>/implementation-loop/git.lock`, never held while an agent works. Two runs that
plan the same issue are not supported: the second one's worktree for it cannot be created.

## Where things live

- Worktrees: `<repo parent>/<repo name>.loop/issue-<root>/` (`coord/` plus one per active issue).
- Run state: `<git common dir>/implementation-loop/issue-<root>/` (`state.json`, `events.jsonl`,
  per-issue evidence). Re-running resumes; a `STOP` file there stops the run.
- Repo-wide claims (merge queue): `<git common dir>/implementation-loop/claims/`.
- The cross-run git lock: `<git common dir>/implementation-loop/git.lock`.
- The private git directories of agent calls in flight: `<repo parent>/<repo name>.loop/issue-<root>/agent-git/`.
- The run's own package caches, the only ones the sandbox binds: `<repo parent>/<repo name>.loop/issue-<root>/cache/`.
- The doctor's canary scratch and the empty hooks directory of the engine's git: `<git common dir>/implementation-loop/canary/` and `no-hooks/`.
- Engine faults waiting to be filed, deduplicated by fingerprint: `<git common dir>/implementation-loop/faults.json`.

## Tests

```sh
python3 -m unittest discover -s tools/implementation-loop/tests -t tools/implementation-loop/tests
```

That runs them one at a time (about 2 minutes). `scripts/check.sh all` and CI spread them over
every core with pytest-xdist; the same by hand, with nothing installed:

```sh
uv run --no-project --python 3.12 --with pytest==8.3.5 --with pytest-xdist==3.8.0 python -m pytest -q -n auto tools/implementation-loop/tests
```

The end-to-end tests drive the real engine, real git, `scripts/ship.sh`, the verifier and real
squash merges into a throwaway bare remote; only the agents and GitHub are fakes. They start
their fake agents and their checks through the engine's sandbox hook with a pass-through
launcher, and with the standing and history checks off (`tests/helpers.py`): their throwaway
repositories hold nothing to isolate and no scanner. `test_sandbox.py` tests the real sandbox by
name, and `test_standing_checks.py` and `test_history_checks.py` run this repository's real
scanner. The real-sandbox tests need a bubblewrap that may create namespaces; without one they
skip, and a skip fails CI's `engine-tests` job.
`tests/fixtures/epic10.json` is a recording of epic #10 of the upstream repository, kept as
recorded: its issues describe that repository, and the tests use them as graph and parser input
only (`scripts/implement.sh record-fixture <issue> <file>` makes such a recording).
The pytest-specific verifier tests are skipped when pytest is not installed.
