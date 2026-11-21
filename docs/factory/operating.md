# Operating the implement loop

How to launch, watch, stop and retry the loop in this repository, what its outcomes mean and
where its state lives. The decision behind it is [ADR 0001](../adr/0001-software-factory.md); the
engine's own reference is [`tools/implementation-loop/README.md`](../../tools/implementation-loop/README.md).

The loop takes a GitHub epic or issue and works every open sub-issue in dependency order. Claude
Fable plans and builds each issue, Codex examines the exact commit, and the engine (plain Python)
owns git, GitHub, the verifier and the merges. A run never waits for a person.

## Before the first run

The loop is inert until the owner has gone through [`green-light.md`](green-light.md): it needs
this repository on GitHub with `main` pushed, the `required` check and the PR-only ruleset in
place, and issues to work on. Until then `scripts/implement.sh doctor` fails, on its `repository`
line (no `origin` remote) or on its `base branch` line (nothing pushed yet).

On a repository where nothing is pushed, `run` and `supervise` do the same thing, which is
nothing: each runs the doctor's checks first, prints the lines that failed and exits with status
1. That happens before it reads an issue, creates a worktree, starts an agent or writes anything
to GitHub, and `supervise --detach` refuses in the foreground, so no background process is left
behind. The checks are read-only (`gh auth status`, `gh repo view`, `git ls-remote origin`,
`git var`) except for the sandbox canary, which works in a scratch directory under
`.git/implementation-loop/` and removes it. The same holds for any other failing line: a commit
identity the hygiene scanner refuses, a credential in the repository's git configuration, a
sandbox that cannot be built.

On the machine that runs it:

- `git`, Python 3.11 or newer, `uv`, the GitHub CLI logged in (`gh auth status`), and the Claude
  Code and Codex CLIs, both logged in. There is no single-vendor mode.
- No AWS credentials within reach of the agents. No agent needs any: nothing in a run calls AWS.
  Every agent call and every check command runs in a sandbox where `~/.aws`, the `gh` login and
  `~/.ssh` do not exist and no `AWS_*` variable arrives. The doctor's `sandbox` line proves it on
  this host with a canary, and fails otherwise. [`isolation.md`](isolation.md) says what the
  sandbox is and what it does not protect.
- A git identity that may be published. The engine commits as this repository's `user.name` and
  `user.email`, and every commit of a pull request is scanned with its author. The doctor's
  `commit identity` line fails while that identity is one the hygiene scanner refuses: use the
  GitHub no-reply address ([`green-light.md`](green-light.md), step 2).

```sh
scripts/implement.sh doctor           # every line PASS or WARN, no FAIL
scripts/implement.sh doctor --smoke   # also one tiny real call to each vendor (costs cents)
```

A `WARN` does not stop a run. A `FAIL` does: the run refuses to start and prints the failed lines.

## Launch

From Claude Code, the `implement` skill does all of this: `/implement <issue>`. It runs the
doctor, shows the plan, asks once for the confirmation below, starts the supervisor in the
background and reports when it ends. By hand:

```sh
scripts/implement.sh plan <issue>                                         # order, waves, warnings; read-only
scripts/implement.sh supervise <issue> --operator claude --yes --detach   # run until settled
```

- `<issue>` is an epic (all its open sub-issues) or one issue (plus its open prerequisites).
- `--operator claude` makes Claude the builder and Codex the examiner. That assignment is a
  decision of this repository (ADR 0001, decision 2); do not launch with `--operator codex`.
- `--yes` records the one confirmation a supervision needs: **the runs merge their own pull
  requests** once every gate passes (the verifier on the exact commit, the examiner's certificate
  for that commit, the `required` check green on it). Once the deployment pipeline exists, a
  merge is also a deployment to the real AWS account.
- `--detach` keeps the supervisor running after the terminal closes. It prints its process id
  and the path of its log.

A supervision is bounded: at most 10 engine runs and 24 hours. `--max-runs` and `--max-hours`
can lower the bounds, never raise them. Supervising the same issue again resumes where it
stopped.

`scripts/implement.sh run <issue> --operator claude --yes` is one engine run without the
supervisor. It resumes an existing run too; nothing restarts it when it ends with issues waiting.

## Watch

```sh
scripts/implement.sh status <issue>
```

One line per issue: its phase, the agent calls charged to it, failed attempts, open findings, its
pull request, and what it waits for or why it failed. Then the totals: number of agent calls, the
Claude cost in dollars, the Codex tokens.

Elsewhere:

- The supervisor's log and the engine's log, in the run directory (see "Where state lives").
- On GitHub: each issue carries a `loop:*` label for its phase and one progress comment the
  engine keeps up to date; the root issue gets the run summary; each pull request has an evidence
  comment and a "Decisions" section listing every choice the loop made on its own.

An issue moves through `pending → plan → planned → build → verify → examine → land → merged`, then
ends in one of the outcomes below.

## Stop

```sh
scripts/implement.sh stop <issue>
```

This asks the engine run in progress to stop at its next safe point: it finishes its current
steps, saves and exits. It does not stop a supervisor, which would start the next run after a
pause. To end a supervision, stop the supervisor first, then the run:

```sh
kill <supervisor pid>                 # printed at launch; the process `implement.py supervise <issue>`
scripts/implement.sh stop <issue>
```

Nothing is lost either way: the state is saved after every transition, and the next launch
resumes from it.

## Retry

```sh
scripts/implement.sh retry <root> [<issue> ...]
```

Gives each `failed` issue fresh ceilings (calls and dollars) and lets each `waiting` issue resume
at once, on the next run. Without issue numbers it applies to every failed or waiting issue of
that root. It refuses while a run is active.

Two other ways to get an issue moving again:

- Edit the issue. A changed body is a changed requirement: the next run replans it with fresh
  budgets. This is also how the owner overrides a decision the loop published.
- `scripts/implement.sh run <issue> --operator claude --yes --fresh` archives the run's state and
  starts over. Use it only on purpose.

## Outcomes

| Outcome | Meaning | What happens next |
| --- | --- | --- |
| `done` | Merged, verified again on `main`, every ledger item has evidence. The issue is closed with its checkboxes ticked. | Nothing. |
| `delivered` | Merged, and every item agents can prove is proven. Typed obligations remain, written on the issue: an `owner` item (it needs something this host lacks, listed in `loop.toml [host] lacks`), or an item deferred to another open issue. The issue stays open with the `loop:delivered` label. | The owner discharges owner items. A deferred item reopens by itself when its issue lands. |
| `waiting` | Not an end state. The issue waits for something that clears by itself. | The supervisor resumes it. `retry` resumes it at once. |
| `failed` | Not delivered, and nothing partial was merged. `status` shows the cause, one of the four below. It is filed as a loop fault (`loop:engine-bug`). | No repair run: the fault stays filed for the operator. A new engine version revives the issue once. `retry` gives it fresh ceilings. |

Why an issue `failed`, and what the operator does about it:

| Cause | What happened | What to do |
| --- | --- | --- |
| `ceiling` | The issue spent its call ceiling, its dollar ceiling, or its budget of attempts that timed out or crashed. The detail says which. | Read the fault issue and the last rounds in `events.jsonl`. If the issue is too large, split it or correct its size label: an edited issue starts with fresh budgets. Otherwise `retry`. |
| `no_progress` | Two rounds, a replan, and two more rounds ended on the same failure. The detail quotes it. | Usually the issue asks for something that cannot be shown here, or says it ambiguously: edit the issue. If the failure is a red `required` check, fix its cause first. Then `retry`, or let the edit resume it. |
| `history` | The branch's own commits carry something the public-repository scan refuses: a value that reached a commit (even one a later commit removed), a commit message, or an author's address. No later commit can take that back, so the engine did not ask the builder to: it abandoned the branch **unpushed**. The commits that carry the finding never reached GitHub. | The fault issue names the file, the line and the rule, never the value. To see it, read the abandoned branch locally (`git log -p <branch>`), then delete it (`git branch -D <branch>`), and close its pull request if an earlier, clean state of it had been pushed. `retry` builds the issue again on a new branch from `main`. If the finding is an author's address, fix the git identity first (the doctor's `commit identity` line). |
| `main_broken` | A merged change broke `main`, one fix-forward did not repair it, and the revert could not be made, verified, certified or merged; or the issue broke `main` twice and was reverted twice. The detail says which. | Look at `main` first: unless the detail says the revert landed, it is still broken and needs a pull request by hand. Then edit the issue or `retry`. |

What a `waiting` issue waits for (`status` shows the kind):

| Kind | Waiting for |
| --- | --- |
| `merge_queue` | Another run is landing; this one lands next. |
| `claim` | Another run is working on this issue. |
| `prerequisite` | A prerequisite in this run is not delivered yet. |
| `ci` | The `required` check gave no verdict in time. |
| `outage` | A vendor CLI could not reach its service, or a model refused the call (usage or rate limit) and its fallback refused too, or it has none. The supervisor resumes once the vendor answers a smoke call. |
| `github` | GitHub kept failing after retries. |
| `environment` | The host cannot run the issue's checks (a missing tool, for example). It resumes when the doctor's probes change. |
| `engine` | The engine itself faulted; a `loop:engine-bug` issue was filed, and the supervisor gives it one repair run (never on the safety surface). It resumes on a new engine version. |
| `run_ceiling` | The run's own call ceiling stopped new calls. The next run continues. |

"Merged" means the gates passed on the pull request. It does not mean the change works in AWS.
An item about runtime behaviour that only a deployed environment can show ends as an owner
obligation on a `delivered` issue, not as `done`.

Budgets, per issue and kept across runs:

- a ceiling on answered agent calls by size label: S 20, M 30, L 45;
- a ceiling on the dollars Claude reports for its calls: S $40, M $70, L $120. Codex reports
  tokens, not dollars, so its side is bounded by the call ceiling only;
- one and a half times both for an issue that touches the factory's own tooling or carries
  `loop:infra`;
- a separate budget of 10 attempts that timed out or crashed. A vendor outage is charged to
  none of them, and neither is a call a model refused because the account's usage limit for it
  is reached: `plan` and `build` then run on Claude Opus (`loop.toml`, `claude_fallback_model`)
  and go back to Fable 30 minutes later, and an escalated Codex call (below) runs on
  `gpt-6.2-sol` and goes back to `gpt-6.2-astra` 30 minutes later (`codex_fallback_model`).
  `agents.jsonl` names the model of every attempt that asked for one, and marks the escalated
  ones.

Codex calls run on the host's default Codex model, except the steps that decide about a
protected path (`PROTECTED_*` in `config.py`), which are escalated to `gpt-6.2-astra`
(`loop.toml`, `codex_escalated_model`; ADR 0001, amendment 12): deciding requests among which is
a `protected_change`, every arbitration, and examining a commit whose change against its base
adds, changes or removes a protected path.

Each Claude call also stops at a budget of its own, by role (`loop.toml`: explore $2, plan $8,
build $25); a call that stops there is charged and not tried again. The dollar figures are
provisional: they are set from what the first epic is measured to cost (ADR 0001, decision 8).
`status` shows what each issue has cost so far. Spending any ceiling ends the issue `failed`
with the cause `ceiling`.

## Where state lives

Nothing below is inside the working tree, and none of it is committed.

| What | Where |
| --- | --- |
| Run state, keyed by the root issue: `state.json`, `events.jsonl`, `engine.log`, `supervisor.log`, per-issue evidence under `issues/` | `<git common dir>/implementation-loop/issue-<root>/` (the git common dir is `.git/` of the main checkout) |
| Every agent call: prompt, answer, schema under `calls/`; one line per attempt with duration, cost and tokens in `agents.jsonl` | the same run directory |
| A stop request (`STOP`) and the lock of an active run (`lock`) | the same run directory |
| Archived state of a run restarted with `--fresh` | `<git common dir>/implementation-loop/issue-<root>.archived-<time>/` |
| Supervisor state (which faults got their repair run) | `<git common dir>/implementation-loop/supervisor-<root>.json` |
| Claims shared by every run (the merge queue, issues being worked on) | `<git common dir>/implementation-loop/claims/` |
| Engine faults waiting to be filed | `<git common dir>/implementation-loop/faults.json` |
| The lock that serializes shared git writes across runs | `<git common dir>/implementation-loop/git.lock` |
| Worktrees: `coord/` and one per active issue | `<repo parent>/<repo name>.loop/issue-<root>/` |
| Private git directories of agent calls in flight | `<repo parent>/<repo name>.loop/issue-<root>/agent-git/` |

Settings a run reads from the tree: `tools/implementation-loop/loop.toml` (models, effort,
timeouts and the budget of one call per role, what the host lacks, the safety invariants, the
sandbox's allow-list), `implement_loop/config.py` (the ceilings, the standing and history checks,
the shared and protected paths, the safety surface) and `prompts/`. All three are
protected paths, and `loop.toml` and `config.py` are on the safety surface.

The run directory holds full prompts and answers, which include issue text and file contents. It
stays on the machine; only what the engine publishes (comments, pull request bodies, fault
issues) reaches GitHub.
