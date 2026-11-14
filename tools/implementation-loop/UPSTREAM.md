# Upstream

This directory is a vendored copy of the implement loop engine. It is not a submodule or a
subtree: fixes are carried between the two copies by hand
([ADR 0001](../../docs/adr/0001-software-factory.md), decision 1 and alternative 7).

| | |
| --- | --- |
| Source repository | <https://github.com/GuilhemBusset/coding-agent-cursus> |
| Commit | `a0dc6df7f9744dc91a57b81acda2102e3394bafa` (2026-11-08) |
| Engine version at that commit | 1.0.7 |
| Vendored on | 2026-11-11 |
| Engine version here | 1.2.1 |
| Licence | MIT, Copyright (c) 2026 Guilhem Busset: the same licence and holder as this repository's `LICENSE` |

Taken from that commit: `tools/implementation-loop/` whole, `scripts/implement.sh`,
`scripts/ship.sh`, `scripts/check-loop-guard.sh`, `scripts/test-ship.sh`, and the `implement` and
`ship` skills (`.claude/skills/` and `.agents/skills/`). Every file not listed below is
byte-identical to upstream. To see the divergence, export the commit and compare:

```sh
git -C <upstream clone> archive a0dc6df tools/implementation-loop scripts .claude/skills .agents/skills | tar -x -C <empty dir>
git diff --no-index <empty dir>/tools/implementation-loop tools/implementation-loop
```

**Every change to the engine extends the tables below**, in the same change: the file, what
changed, and why. That includes tests removed or adapted.

## Local patches

### 1.1.0: the port (2026-11-11)

Upstream was written for a repository of course material: session folders, HTML pages checked in
a browser, Node tooling. The port removes that coupling and sets this repository's constants. The
state machine, the gates, the budgets and the supervisor are unchanged.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/__init__.py` | `VERSION` 1.0.7 → 1.1.0. | A new engine version for the port. |
| `implement_loop/config.py` | `SHARED_FILES` are this repository's (`README.md`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `.gitignore`, `pyproject.toml`, `uv.lock`, `cdk.json`). `PROTECTED_PREFIXES` gains `tools/hygiene/` and `tests/policy/`; `PROTECTED_FILES` gains `scripts/check.sh` and loses `setup/homework-branches.json`. The safety surface gains `scripts/check.sh` and `tools/hygiene/` and loses `setup/homework-branches.json`. `REPO_TOP_LEVEL` is removed. `SHARED_PREFIXES`, `INFRA_PREFIXES`, the ceilings and the caps are unchanged. | ADR 0001, decision 10. `REPO_TOP_LEVEL` only served session-relative paths. |
| `implement_loop/issue_parser.py` | Removed `resolve_path`, `mentioned_session_dirs`, `session_dir_for` and the `session_dir` and `existing_session_dirs` parameters of `declared_paths` and `parse`. A backticked path in an issue is repo-relative, as written. `sections`, `ledger`, `normalize_body`, `body_hash`, `reference_paths`, `suggested_branch` and `tick` are unchanged. | No `session:NN` labels or `sessions/` folders here. |
| `implement_loop/graph.py` | Removed `existing_session_dirs`; `build_plan` calls `issue_parser.parse(issue)`. | Same. |
| `implement_loop/engine.py` | Two calls of `issue_parser.parse` lose their second argument; one docstring example path. Nothing else. | Follows the parser. |
| `implement_loop/environment.py` | Removed the Node and npm probe, the browser probe, the staged `npm ci`, the browser install, the shared-library recipe, `browser_env`, and the state-directory parameter of `probe` and `heal`. Kept: the pytest probe through uv, `heal` (it now only reports a missing `uv`), `fingerprint`, `host_lacks`, `smoke_call`, `vendor_reachable`. | This repository has no browser tooling and none of the upstream `setup/` recipes. |
| `implement_loop/cli.py` | `probe` and `heal` are called without a state directory; `environment_dir` is removed; `prepare_environment` returns an empty `check_env`. | Follows `environment.py`. |
| `implement_loop/cli.py` | Two additions to the doctor, the only new behaviour of this version. `repository_problem`: when there is no slug, the `repository` line says that the `origin` remote is missing, if it is, instead of "gh repo view failed". `base_branch_check`: a new `base branch` line that fails while the remote has no `main` (`git ls-remote --exit-code --heads`). A run already refuses to start on any doctor failure, so it now refuses on an empty repository too. | The doctor has to fail, and say why, while the factory is switched off (ADR 0001, decision 11). The owner's GitHub repository existed, empty, before anything was pushed, so "no remote" alone did not cover it; upstream would have failed later, at the first fetch. |
| `implement_loop/workspace.py` | Removed `provision` (the copy of `setup/node_modules` into each worktree) and its three calls. | Nothing to provision. |
| `implement_loop/live.py`, `implement_loop/verify.py` | One docstring sentence each no longer cites the browser's libraries as the extra environment. No code change. | Wording. |
| `implement_loop/loop_pytest.py`, `implement_loop/scheduler.py` | One docstring example or sentence each. No code change. | Wording. |
| `loop.toml` | `[roles.plan]` and `[roles.build]` set `claude_model = "claude-fable-5-2"`. `[host] lacks` and `[invariants] names` are this repository's: the seven upstream invariants plus four. | ADR 0001, decisions 2, 6 and 9. |
| `prompts/_common.md`, `build.md`, `plan.md`, `examine.md`, `decide.md`, `triage.md` | Wording for this repository: Python CDK infrastructure, synthesis with no credentials and no lookups, assertions on the template, invariants in `tests/policy/`, nothing that identifies the account, no AWS calls, what only a real account can show is an `owner` item. Two instructions are new: the builder runs `scripts/check.sh fast` before it answers `done`, and the request decider approves a change to a protected path only when the issue's own text asks for it. The placeholders and the answer formats are unchanged. `arbitrate.md`, `explore.md` and `resolve_locked.md` are unchanged. | Upstream wording was about course material, HTML pages and a browser. |
| `README.md` | Rewritten where it described the upstream repository; `UPSTREAM.md` (this file) is new. | |

Outside this directory:

| File | What changed | Why |
| --- | --- | --- |
| `scripts/implement.sh`, `scripts/check-loop-guard.sh` | Header comments only: the usage lines, and the ADRs they point to. | The upstream ADR files are not in this repository. |
| `scripts/ship.sh`, `scripts/test-ship.sh` | Unchanged. | |
| `.claude/skills/implement/SKILL.md`, `.agents/skills/implement/SKILL.md` | Rewritten for this repository: the operator is always `claude`, what the host lacks, no AWS, the doctor's `repository`, `base branch` and `sandbox` lines. | ADR 0001, decisions 2, 3, 6 and 11. |
| `.agents/skills/implement/agents/openai.yaml`, the `ship` skill | Unchanged. | |

Kept on purpose, although it looks like upstream's stack: `implement_loop/verify.py` still skips
`node_modules` (with `.git`, `.venv` and `__pycache__`) when it looks for `conftest.py` files, and
`engine.py` still treats "`node`, `npx` or `npm`: command not found" and two browser-launch
messages as a host problem, not a defect of the change. Both are verifier and state-machine
behaviour, and the CDK CLI is a Node program.

### 1.2.0: the sandbox, the standing and history checks, the ceilings (2026-11-12)

What ADR 0001 adds to the engine, except the deployment status of its decision 5 (see "Not in
this version"): every agent call and every check command runs in a sandbox that holds no AWS
material and no GitHub login (decision 3); the public-repository scan is a check of the verifier,
on the tree and on the branch's history (decision 4); spend has a dollar ceiling (decision 8); a
protected path is granted only when the issue names it (decision 10). What the sandbox does not
protect is in [`docs/factory/isolation.md`](../../docs/factory/isolation.md).

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/__init__.py` | `VERSION` 1.1.0 → 1.2.0. | A new engine version: a failed issue is revived once by it. |
| `implement_loop/sandbox.py` | New. `Bubblewrap.command` builds the `bwrap` line: the root filesystem read-only; `/tmp`, the `hide` directories, `$HOME` and the home directory the password database gives the account replaced by empty ones; then the allow-list bound back (the toolchain read-only, the caller's `Mounts`, the running vendor's login file read-write and its settings read-only); PID, IPC and UTS namespaces, a session of its own, death with the parent, the network shared. `load_policy` reads `loop.toml [sandbox]` and refuses a malformed table. `worktree_mounts` decides what a call sees of a worktree and of git from the workspace's own paths, never from a file an agent can write. `clean_env` is the one environment scrub: by name, without regard to case, and by the shape of the value. `engine_git_env` is what a run pins for the engine's own git: no hook, no file-system monitor, no `ext::` transport. `canary`, with the probe it starts inside, judges the real sandbox for the doctor. `use` and `PassThrough` let the tests replace the launcher; the engine never calls them. | Decision 3. One module, so that the patches to `live.py` and `verify.py` stay small. The engine's git is pinned because it runs outside the sandbox in worktrees an agent wrote to: with `core.hooksPath` set to `.githooks`, it would run the builder's copy of a hook as the owner. |
| `implement_loop/live.py` | `_spawn` and `_codex_mcp_off` start their process through `_sandboxed`. `_mounts` names what each role sees: its worktree (read-only for a read-only role), the run's package caches for a working role, the schema and answer files for Codex. `agent_env` is `sandbox.clean_env` and takes the vendor. A sandbox that cannot be built is reported to the meter as `unavailable` and raised: nothing ran, nothing is charged. `RoleSettings.max_budget_usd` becomes `--max-budget-usd` on Claude calls; a call that ends with `error_max_budget_usd` is charged and not retried; what each attempt cost goes to `meter.spent`. `CLAUDE_DENY` gains `Bash(aws *)` and `Bash(cdk deploy*)`. `_parse` no longer raises on a JSON answer that is not an object. | Decisions 3 and 8. The deny list is a convenience, as before. |
| `implement_loop/verify.py` | `_run` starts the command through the sandbox, with standard input closed. `run_check` and `verify` take `mounts`, to which `run_check` adds the engine's directory read-only (the result plugin), the record directory and the run's caches. `scrubbed_env` is `sandbox.clean_env`. New `standing_checks`, which `verify` runs before the issue's commands: a check that does not exit 0 is a problem that carries its output, never an exception. | Decisions 3 and 4. A failing scan must go back to the builder with its output, and a failing push would be an engine fault. |
| `implement_loop/environment.py` | The smoke call and the reachability call (`_runner_agents`) go through the sandbox, with the vendor's scrubbed environment. `vendor_reachable` answers False where the sandbox cannot be built. | Decision 3: the third place an agent is started. |
| `implement_loop/config.py` | `sandbox.py` joins the safety surface. `.claude/`, `.agents/` and `.codex/` (`AGENT_CONFIG_PREFIXES`) join the protected paths and the safety surface. `STANDING_CHECKS` (the hygiene scan of the tree) with `standing_checks()`; `HISTORY_CHECKS` (the same scanner over `<base>..<head>`), `HISTORY_FINDINGS_EXIT` and `history_checks()`; `USD_CEILINGS` (S 40, M 70, L 120, provisional) with `usd_ceiling()`. | Decisions 4, 8 and 10; amendment 6 for the agents' configuration, where a hook or a setting a builder adds would run in the owner's own session, outside the sandbox. |
| `implement_loop/engine.py` | **The meter.** `spent` charges what a vendor reports to the issue's budget; `before` refuses a call once the dollar ceiling is spent, and the issue fails with the cause `ceiling`; a revival and a retry grant fresh dollars, an owner's edit resets them. **The sandbox.** Every `verify` and `run_check` call passes `mounts` (`_mounts`); `SandboxUnavailable` makes the issue wait (`environment`) instead of being an engine fault. **Standing checks.** `verify` gets them on every head it verifies (not for the regression re-runs of other issues, on a head already scanned) and a revert passes them before it is pushed. `_uncommittable` runs them on the builder's working tree before the engine commits a round or a checkpoint: a refused round is not committed (`build_not_committed`, `update_postponed`) and goes back with the output. `_unclean_probes` runs them on the examiner's probes before they are run or adopted; the examiner is told which were not kept and why. **History checks.** They run with the standing checks on every head `_step_verify` verifies, and `_history_scan` runs them again before each push (`_step_land`, `_revert`). On a finding, `_history_refused` abandons the branch unpushed, starts a new round on a new branch and ends the issue `failed` with the cause `history`; a check that could not run is a failed check (`push_withheld`, then a round under the progress rule). `IssueState.restarts` counts rounds abandoned that way, so that `_revert` still finds the merged pull requests of the rounds around them. **Protected zones.** `_step_plan` grants a protected path among the plan's zones only when the issue's deliverables name it, or name a protected directory above it; any other is withheld (`protected_zones_withheld`) and the builder is told to ask with a `protected_change` request. | Decisions 3, 4, 8 and 10. A value that reached a commit stays in the branch whatever later commits do, and CI's `history` job refuses it: no round of the builder can repair that, so the engine does not ask for one. Upstream granted every protected zone of a plan because "triage saw it", but triage has no objection kind for a zone. |
| `implement_loop/model.py` | `IssueState.restarts`. | See `engine.py`. |
| `implement_loop/issue_parser.py` | `.codex/` joins the dot-directories whose backticked paths count as paths (`_DOT_DIRS`). `sections` and `ledger` are unchanged. | So that an issue can name a path under `.codex/`, now protected, as it can under `.claude/` and `.agents/`. |
| `implement_loop/cli.py` | The doctor gains four lines: `sandbox` (bubblewrap, then the canary; a failure), `commit identity` (the author and committer git would commit as, judged by the hygiene scanner itself through `scan.py --redact`; a failure), `git config` (no credential in the repository's own configuration; a failure) and `node` (a major the CDK CLI supports; a warning). `doctor --canary` prints everything the canary saw. `preflight` is shared by `run` and `supervise`, which now refuses up front instead of launching runs that refuse. `cmd_run` pins the engine's git (`sandbox.engine_git_env`) and gives checks and agents the run's own package caches (`prepare_environment`), whose fingerprint includes whether the sandbox can be built. `host_lacks` is judged on the scrubbed environment. `status` shows each issue's dollars and `retry` grants fresh ones. | Decisions 3, 8 and 11. The engine commits as the repository's git identity, and the history scan reads the author and committer of every commit: with an address it refuses, no branch could land. |
| `loop.toml` | `max_budget_usd` per role (explore 2, plan 8, build 25, provisional). The `[sandbox]` tables: `toolchain`, `hide`, `login`, `settings`. No path had to be added for either vendor's CLI to run in the allow-list form on the host it was proved on. `[host] lacks` gains `host:container-runtime`: the host has no container runtime, tests that start a container run only in CI's `container-tests` job, and that job's result is what discharges the item (the engine does not read it yet). No test of the engine pins the list of capabilities, so none changed. | Decisions 3 and 8; amendment 5 for the capability, which the backlog's drafts name. |
| `prompts/plan.md` | The sentence on zones says what the engine now does with a protected path. | It described upstream's grant at triage. |
| `prompts/examine.md` | Two additions: a probe holds nothing the public-repository scan refuses, and each `hygiene: allow` marker a change adds is to be read as a possible finding. | A marker lets a value past the scan, also when a builder writes it; the scanner's own documentation relies on the examiner for that, and the prompt did not say so. |
| `README.md` | The doctor's lines, the sandbox, the standing and history checks, the dollar ceilings. The warning not to launch near AWS credentials is removed. | |

Outside this directory:

| File | What changed | Why |
| --- | --- | --- |
| `docs/factory/isolation.md` | New: the isolation model, the allow-list, how to check it on a host, and what is not protected. | ADR 0001, decision 3 and its consequences. |
| `docs/factory/operating.md` | What `run` and `supervise` do on a repository where nothing is pushed; the sandbox and the commit identity among what a run needs; the dollar ceilings among the budgets; the four causes of `failed`, `history` among them, with what the operator does about each. | `supervise` used not to run the doctor, and the page described the budgets and the outcomes of 1.1.0. |
| `.claude/skills/implement/SKILL.md`, `.agents/skills/implement/SKILL.md` | The preflight names the doctor's `commit identity` and `sandbox` failures; what the operator tells the user before a launch gains the dollar ceilings and says that agents run in the sandbox; the final report says what a `history` failure means. The two copies stay byte-identical. | They described an engine with call ceilings only and no sandbox. |

Behaviour an upstream reader would not expect, beyond the additions: the engine's git runs no hook
during a run; a plan's protected zone is not granted by the examiner's silence; `supervise` runs
the doctor first; check commands have no standard input.

**Not in this version.** The deployment status of ADR 0001, decision 5: no `deploy_status`, no
`[deploy]` table, no `deploying` phase. It arrives with issue #12 (draft E0-07), in the patch
"the deployment verdict" below.

### 1.2.0, patched: the pipeline stack and the bootstrap policies are protected (2026-11-14)

A merge to `main` is deployment rights in the AWS account, so ADR 0001's first amendment adds the
pipeline stack and the bootstrap policies to the protected paths. Issue #8 (draft E0-02) does it
before either folder exists: a prefix protects whatever lands under it. The engine's version stays
1.2.0: a new version revives every failed issue once, and nothing here repairs one.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/config.py` | `PROTECTED_PREFIXES` gains `src/rhi/pipeline/` and `setup/bootstrap/`. `PROTECTED_FILES`, the safety surface (`SAFETY_SURFACE_FILES`, `SAFETY_SURFACE_PREFIXES`) and every other constant are unchanged. | ADR 0001, amendment 1. Neither folder joins the safety surface: the amendment does not ask for it, and a fault-repair run whose issue names one may change it. |
| `prompts/plan.md`, `prompts/decide.md` | The sentence that lists the protected paths names the pipeline stack under `src/rhi/pipeline/` and the bootstrap policies under `setup/bootstrap/`. The placeholders and the answer formats are unchanged. | The prompts repeat the engine's list for the builder that plans and for the examiner that decides a request. |

Outside this directory:

| File | What changed | Why |
| --- | --- | --- |
| `AGENTS.md` | "Protected paths and the safety surface" names the two folders in place of "the pipeline definition, once it exists". | The section repeats the engine's list for people. |

### 1.2.1: a probe file the file system refuses (2026-11-14)

The repair of the engine fault a25bef17ec (issue #243). An examiner named a probe file that no file
system can create, and the lint of the probe raised `OSError` where it writes the probe to a
temporary directory: the examination crashed, and the issue waited for a new engine version.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/__init__.py` | `VERSION` 1.2.0 → 1.2.1. | A new engine version: the issues that wait for this repair resume, and a failed issue is revived once by it. |
| `implement_loop/engine.py` | `_probe_problem` answers with a reason when the probe's directory or file cannot be created (`OSError`), in the engine's own words, instead of raising. `_step_examine` adopts only probes that passed the lint, and a finding that names a probe the lint refused loses that reference and stands on its own reproduction. | A path the file system refuses is the examiner's mistake, like the other paths the lint refuses, and not an engine fault. Guarding the lint alone left the same crash one step later: a blocking finding that named the refused probe had it adopted, which writes it to the worktree, and a finding that keeps the name of a probe that is never adopted cannot close. The reason does not repeat what the system said, which carries the path of the temporary directory. |

Behaviour an upstream reader would not expect: a probe the lint refuses for any reason (a protected
path, a test that fails unconditionally) is no longer adopted when a blocking finding names it.

### 1.2.1, patched: what the engine publishes is redacted (2026-11-14)

The engine publishes on GitHub: evidence comments with the tail of a command's output, pull request
bodies, fault issues, follow-up issues, progress comments that quote why an issue waits or failed.
A command's output can carry an account ID or an ARN that no source file holds. Issue #10 (draft
E0-04) passes every such text through the hygiene scanner's redaction first, a follow-up that ADR
0001 lists under its decision 4. The engine's version stays 1.2.1: nothing here repairs a failed
issue, and the wait this patch uses (`github`) resumes on any run.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/redact.py` | New. `redact(text)` runs `tools/hygiene/scan.py --redact` on the text and returns what the scanner wrote. The scanner is the one of the checkout the module is in, found from the module's own path. It runs with the engine's environment. A scanner that is missing, exits non-zero, cannot be started or does not answer within 60 seconds raises `RedactionError`, whose message holds nothing of the text and nothing the scanner wrote. | The engine runs outside the sandbox, as the owner. The scanner it trusts cannot be the copy in an issue's worktree, which a builder can write, nor one found through the working directory. With the owner's environment the scanner also masks the owner's local values where their list exists. There is no fallback: a text that could not be masked is not returned. |
| `implement_loop/github.py` | New: `RedactingGitHub`, a client that wraps another one and masks the body of `create_comment`, `update_comment` and `update_pr`, and the title and the body of `create_pr` and `create_issue`, before the inner client is called; every other method is forwarded as it is, `edit_issue` among them. New: `RedactionUnavailable`, a `GitHubUnavailable`, raised when the redaction failed, before anything is sent. `GhCli`, `FakeGitHub` and `RecordingGitHub` are unchanged. | These five calls are every title and body the engine sends. `edit_issue` closes an issue or ticks its boxes; a tick sends back the issue's own text, and the engine never rewrites what the owner wrote. The engine already turns `GitHubUnavailable` into the wait `github`, which the supervisor resumes, so no new kind of wait is needed. |
| `implement_loop/cli.py` | New: `github_client(slug)`, which returns `RedactingGitHub` around `GhCli`. `cmd_run`, `cmd_plan` and `cmd_record` build their client with it; `GhCli` is constructed nowhere else. | A run, and so every run the supervisor launches (it has no GitHub call of its own), publishes only through the redacting client. |
| `implement_loop/engine.py` | `_safe`, which journals a failed GitHub call and goes on, raises `RedactionUnavailable` instead of swallowing it. In a step (`_land`, `_accept`) the step ends there and `_finished` makes the issue wait with the kind `github`, in the phase that publishes again. `_sync` catches it, because a phase also changes outside a step: it notes the issue in `_unpublished`, and `_withhold`, called when the issue's step ends and once more at the end of the run, makes the issue wait unless it already waits or has ended. `_accept` publishes the comment about deferred items before the follow-up issue, which is recorded as soon as it is created, and writes the progress comment of the phase the issue ends in before that phase is recorded and before the issue is closed (`_announce`); `_reconcile_delivered` does the same before it closes an issue the owner ticked; when it cannot, the issue stays open and waits, to resume as `delivered`. `_flush_faults` no longer only journals a fault issue (or its "seen again" comment) that could not be masked: the fault's issues of this run wait as they are, `fail` kept, and the fault stays in the outbox. `_withhold` makes a failed or a delivered issue wait as well. `_reconcile` gives such an issue its ended phase back before anything else (`_restore_ended`), so an edit still starts a new round and a new engine version still revives it, then writes its labels and its comment again. `_post_run_summary` journals the failure. The import of `RedactionUnavailable` has the same fallback as that of `GitHubUnavailable`. | Raising `GitHubUnavailable` from the client is not enough: the evidence comment, the follow-up issue, the comment about deferred items and the progress comment are written through `_safe`, so `_accept` would have closed an issue whose follow-up was never filed, and no later call would have made it wait. The issue's deliverables name `github.py` and `cli.py` and not this file; the change was asked for with a `protected_change` request. An ended issue has to wait too: the supervisor counts a failed or a delivered issue as settled and stops, so a fault issue or a last comment that was not published would never be. It waits with its ended phase as the phase to resume in, so nothing it recorded is lost and it is not worked again. Three cases keep their state on purpose: an issue that already waits for another reason keeps that reason (a waiting issue is never settled, so a next run comes), a done issue stays done (its comment was written before it was closed), and the run summary on the root issue belongs to no work item. A fault none of whose issues can wait (they are done, or belong to another run) stays in the outbox and is journaled, as when GitHub fails. In all of these nothing is published, and the next run that can mask writes the text. |

Behaviour an upstream reader would not expect: what GitHub receives from the engine can differ
from what the engine rendered, by the scanner's masks (`[redacted <rule>]`) and by line endings
(the text passes through the scanner's standard input and output, so `\r\n` arrives as `\n`);
each publication starts one scanner process; `_accept` updates the progress comment twice; an issue can wait with `failed` or `delivered` as the phase it resumes in.

### 1.2.1, patched again: the deployment verdict (2026-11-14)

A merged change is deployed by the pipeline, and until now nothing told the loop how that went.
Issue #12 (draft E0-07) builds decision 5 of ADR 0001, as its amendment 4 restates it: the pipeline
posts commit statuses on each merge commit, and the engine reads them between the merge and the
evidence step. The table that names them lands with neutral values, so nothing waits until the
owner lists the contexts (`docs/factory/green-light.md`, step 11). The engine's version stays
1.2.1: nothing here repairs a failed issue, and the two waits this patch adds resume on any run.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/github.py` | New: `deploy_status(sha, context)` on the `GitHub` protocol, on `GhCli` and on `FakeGitHub`. `GhCli` reads every page of `commits/<sha>/statuses` and returns the state and the description of the newest status of that context, or None when the commit has none. `FakeGitHub` gains `statuses` and `set_status(sha, context, state, description)`, with which a test plays the pipeline. `RedactingGitHub` and `RecordingGitHub` forward the call like any other read. | The pipeline reports with commit statuses and nothing else (amendment 4). A context is posted again at every step of a deployment, so the latest one is the verdict. |
| `loop.toml` | New table `[deploy]`: `contexts = []`, `timeout_s = 3600`, `approval_description = ""`. | Neutral values: with no context the engine reads nothing and waits for nothing. Setting them is the owner's, through a pull request of their own: the file is on the safety surface. 3600 seconds is a starting point; the design gives no value. |
| `implement_loop/config.py` | New: the dataclass `Deploy` (the three keys, with those defaults) and `load_deploy`, which `load_policy` calls: the policy gains the key `deploy`. A key the table does not have raises `ValueError` and names it; a value of the wrong type raises `TypeError`; no table gives the defaults. | A misspelt key would leave the engine waiting for nothing while the owner believes it waits. `cli.py` passes the policy to the engine as it is, so it is not changed. |
| `implement_loop/model.py` | New phase `Phase.DEPLOYING` (`deploying`). New kinds of wait `WAIT_DEPLOY` (`deploy`) and `WAIT_OWNER_APPROVAL` (`owner_approval`). `IssueState` gains `deploy`: the commit whose verdict decides the round, then the states its contexts reported. | The phase, the wait the issue asks for by name, and a kind for "no verdict within the timeout", so that `status` tells it from a required check with no verdict (`ci`). The merge commit is kept in the state so that a resumed issue reads the same commit and never goes through `land` again. |
| `implement_loop/engine.py` | The five exits of `_step_land` that set `merged` go through `_merged`, which records the commit (the merge commit; the head of `main` when the branch had nothing left to land) and, with contexts configured, calls `_await_deployment` before the land step ends, so before it releases the merge queue. `_await_deployment` sets the phase `deploying` and polls `deploy_status` for every context: all `success` records the verdict (event `deployed`) and returns the issue to `merged`, where `_accept` runs as before and adds one evidence record (criterion `deployment`: the commit and each context) to the evidence comment and to the issue's evidence, a record that ticks no box; one `failure` or `error` goes to `_fix_forward` at once (event `deployment_failed`), with a line that names the commit, the contexts and their states and nothing else a status says; a pending status whose description equals a non-empty `approval_description`, with no context failed, makes the issue wait with `owner_approval`; otherwise, after `timeout_s` of polling, the issue waits with `deploy`. Statuses that cannot be read are no verdict: the issue waits with `github`. A stop request ends the polling and leaves the issue in `deploying`. `_maybe_resume` resumes both new waits on every run, into `deploying`; `_dispatch` gives such an issue the merge queue before any issue in `land`, and `_step_deploying` polls the same commit again. `_step_accept` sends an issue whose verdict is owed to `deploying` instead of running. `_new_round` and `_owner_edit` forget the commit. `_reconcile` treats `deploying` as merged when the issue was edited. | Decision 5: with contexts configured, a merge is accepted only when they report success; a failure takes the broken-main path that exists (one fix-forward, then a revert pull request); no verdict never fails an issue. The phases an issue goes through are `land`, `merged`, `deploying`, `merged` (the evidence step), `done`: `merged` keeps the one meaning it had, and `deploying` means only "the verdict is owed". On a branch with nothing left to land the head of `main` is judged, because that commit carries the change: without it a fix-forward round that changes nothing would end the issue `done` on a deployment that failed. |
| `implement_loop/engine.py` | The landing claim through the wait. A land or deploying step that is running counts as landing whatever phase its issue has reached (`_landers`), so the merge queue stays with it while it polls. For a verdict that outlasts the run, `_hold` writes a record of the awaited commit under `<git common dir>/implementation-loop/deploying/`, one file per issue, before the phase `merged` is recorded; `_take_queue`, which `_dispatch` and `_revert` now use in place of the bare claim, refuses to land while another issue's record exists, whichever run wrote it. The record is removed when the verdict is final, when the issue starts to wait for an approval, and when a new round starts; `_drop_stale_holds` removes, at the start of a run, the records of that run whose issue no longer owes a verdict. An issue ready to land behind a verdict that this run will not read again waits at once (`merge_queue`) instead of polling. | "The landing claim is held through the wait, so one merge maps to one deployment and one verdict." The merge-queue claim cannot do that alone: it belongs to a process, `Engine.run` frees it when the run ends, and `RepoClaims` takes over a claim whose process died, so between a timeout and the supervisor's next run any other run could land. `state.py` is not among the files the issue names, so the record is written by the engine, beside the claims. A deployment that waits for the owner's approval releases the claim on purpose: holding it would stop all landing until the owner acts, and the pipeline queues one execution per commit, so a later merge still gets its own verdict. |
| `implement_loop/engine.py` | `Deploy`, the two kinds of wait and the phase are imported inside a `try`, as `GitHubUnavailable` and `RedactionUnavailable` are, with stand-ins for a `config.py` and a `model.py` that do not have them: no context, and a phase that equals none. The engine names the phase through that import (`DEPLOYING`), and reads `IssueState.deploy` only when a context is configured. | `tests/test_unwritable_probe.py` loads the `engine.py` beside it into the `implement_loop` of the engine that runs the loop (an xdist worker under the verifier, see "Tests removed or adapted", 1.2.1). While this patch is built, that engine has neither the phase nor `Deploy`: without the stand-ins the file does not import there, and `scripts/check.sh engine-tests` fails under the verifier, on a test this patch may not change. Once this patch runs the loop, the `except` branch is dead. |
| `implement_loop/engine.py` | `PHASE_LABEL` maps `deploying` to the label `loop:merged`. At import, the module adds the words of the phase to `report.PHASE_TEXT` and a hint for each of the two waits to `report.WAIT_HINT`. | `_sync` renders the progress comment at every change of phase and the run summary counts issues by phase, both through `report.PHASE_TEXT[phase]`: without an entry the first issue to enter `deploying` would raise `KeyError`. `report.py` is not among the files the issue names, so the entries are added from the module the issue does name; the day `report.py` is changed for another reason they belong there. No new label: an issue that waits for its deployment is merged. |

Behaviour an upstream reader would not expect: with contexts configured a land step lasts until the
deployment verdict or `timeout_s`, and no other issue of any run lands meanwhile; a file under
`<git common dir>/implementation-loop/deploying/` stops every landing for as long as it exists,
with no process behind it, and is removed by the run that wrote it (run that root again, or empty
`contexts`, to free it); an issue in `land` can wait with the kind `merge_queue` at once, behind a
verdict its own run waits for; a round with nothing to land is judged by the statuses of the head
of `main`; the evidence comment can carry a row that is no ledger item (`deployment`); an issue
that waits with `owner_approval` is resumed by every run and waits again until the status changes,
so the supervisor reaches its bounds if the owner does not act, and `status` shows the kind.

### 1.2.1, patched a third time: a container obligation discharged by its CI job (2026-11-14)

An item that rests on a test under `tests/container/` names `host:container-runtime`, which the
host lacks, so until now it ended as an owner obligation on a `delivered` issue, with nothing left
for the owner to do: the CI job `container-tests` runs those tests on the very head that lands, as
part of `required`. Issue #14 (draft E0-19) builds what amendment 5 of ADR 0001 left to the
backlog: when an issue lands, the engine reads that job's check run on the head that was merged
and, if it succeeded, records the item as proven. No capability is added. The engine's version
stays 1.2.1: nothing here repairs a failed issue. The 1.2.0 entry of `loop.toml` above says the
engine does not read the job's result yet; that was so until this patch.

| File | What changed | Why |
| --- | --- | --- |
| `loop.toml` | `[host]` gains `ci_jobs = { "host:container-runtime" = "container-tests" }`: for a capability of `lacks`, the name of the CI job that provides it. The comment on the capability says what the engine now reads and when the item still ends as an obligation. `lacks` is unchanged. | The mapping is configuration on the safety surface, like the list it completes: the engine holds no job name of its own. |
| `implement_loop/config.py` | New `load_ci_jobs`, which `load_policy` calls: the policy gains the key `ci_jobs`, a dictionary from capability to job name, empty when the table has none. A value that is not a non-empty string raises `TypeError`. | `cli.py` passes the policy to the engine as it is, so it is not changed. |
| `implement_loop/engine.py` | `_accept` calls the new `_ci_discharged` before it writes the evidence comment. An owner item is discharged when the plan gives it a capability that `ci_jobs` maps to a job, its text names a Python file under `tests/container/` (`CONTAINER_TEST`: the path from the repository's root, with or without a leading `./`; the folder alone names no test, and neither does the same folder under another one), and the check run of that job's name on the merged pull request's head (`_merged_pull`: the pull request recorded for the round, merged, as `find_pr` returns it) is completed with the conclusion `success`. The check run's address is its page in the repository, `runs/<id>` beside the pull request's own page (`PULL_ADDRESS`): `required_check_run` returns a check run's id, status and conclusion and not its address, and `implement_loop/github.py`, which this issue does not name, is not changed. Such an item gets an evidence record (kind `check_run`, with the commit, the job and the address), a row in the evidence comment, and its box ticked with the check run's address as the evidence link; its obligation is removed, so it is in neither `obligations` nor `human_tasks`, and the pull request's body is rendered again without it under "Left for later". The event `obligations_discharged` journals the head and the addresses. The rest of `_accept` is unchanged: an issue with no obligation left is closed as `done` by the code that was there. | Amendment 5. The head of the merged pull request is the commit `required` was read on before the merge, so the job's run is complete there. The plan is read rather than `obligations` so that the step, which runs again when a publication ends it, records the same evidence the second time. |
| `implement_loop/engine.py` | What discharges nothing, and leaves the obligation as it was: a capability with no mapping (no check run is read for it), an item that names no test under `tests/container/`, a round that merged no pull request, a pull request without a head or an address, no check run of that name, one that is not completed, was skipped or did not succeed, one without an id, and a read GitHub did not answer (journaled as `github_call_failed`). The check run is read once and not waited for. | The owner's obligation is the safe outcome: a false discharge would close an issue on evidence nobody has. |

Behaviour an upstream reader would not expect: an issue with an owner item can end `done` with no
action of the owner; a box of the issue can be ticked with a link that is not the evidence comment;
the evidence comment can carry a row whose proof is a check run; a merged pull request's body is
edited after the merge when an item was discharged. The address is published through the
redacting client like the rest of the comment.

### 1.2.1, patched a fourth time: the owner's local values before every push (2026-11-14)

No public pattern can name the real domain, so the owner keeps such values in a list outside the
repository, and the hygiene scanner reports them as `local-value` where that list exists
(`tools/hygiene/README.md`, "Values only the owner knows"). The loop's pushes were the one path
not held against it: its git runs no hook of the repository, and its verifier scans in the
sandbox, where the list does not exist. Issue #16 (draft E0-20) has the engine, which runs outside
the sandbox as the owner, scan with that list immediately before each push. The engine's version
stays 1.2.1: nothing here repairs a failed issue.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/config.py` | Beside `HISTORY_CHECKS`: `LOCAL_VALUE_CHECKS`, the scanner's arguments for the tree of the head (`--rev {head}`) and for the commits a push would publish (`--history {base}..{head}`); `LOCAL_VALUE_SCANNER` and `LOCAL_VALUE_ALLOW_LIST`, the `scan.py` and the `hygiene.toml` of the checkout this file is in; `local_value_checks`, which makes the argument vectors (the engine's own interpreter with `-I`, the scanner, `--config` and the allow-list by absolute path, no shell), and returns none where `HISTORY_CHECKS` is empty; `local_values_file`, the scanner's own rule for where the list is (`HYGIENE_LOCAL_VALUES` when set, else the default file when it exists). The module now imports `sys`, `Mapping` and `Path` at its top. | The scan runs outside the sandbox, so it may execute nothing a builder wrote: the scanner is named from the engine's own place, as `redact.py` names it, and never from a worktree. Off with the history checks: `tests/helpers.py`, which this issue does not name, turns those off for the suite, and the suite then starts no scan with whatever list the machine that runs it holds. In a run the history checks are always on. |
| `implement_loop/engine.py` | New `_local_scan`, called in `_step_land` after the history scan and in `_revert` after the certificate, each time directly before the push. With no list it starts nothing and returns `None`. With one it runs each command through `_outside` (`subprocess.run` in the engine's repository, with the list named in `HYGIENE_LOCAL_VALUES`, bounded by `check_timeout_s`), and returns places and problems. `local_places` reduces a finding to `<path>: <rule>`: the line number, the blob or commit ID and the excerpt are dropped, and the scanner's output is never journaled, recorded under the run's directory or put into an exception. A scan that ends with another status than 0 or 1, does not end, or cannot be started is a problem in the engine's own words, with the exit status and nothing the scanner wrote. | The issue's deliverables. Here the excerpt surrounds the owner's value itself, and the line number says where in a file to look for it. |
| `implement_loop/engine.py` | `_history_refused` takes `places`: given, they are journaled as the findings with an empty `output`, the failure's detail names the scan with the owner's local values and says that such a value comes from text the owner wrote, and the rest is as for a history finding (the branch abandoned unpushed, `failed` with the cause `history`, the next attempt on a new branch). In `_step_land` a problem journals `push_withheld` and fails the round, as an unrunnable history check does. In `_revert` a finding or a problem ends in `main_broken`, and the revert is not pushed. | A finding of this scan is handled as a finding of the history check is. Existing callers pass no `places` and are unchanged. |
| `implement_loop/engine.py` | New `_push`, which both steps call in place of `Workspace.push`, and `_own_hooks`. For a scanned head, `_push` writes a `pre-push` hook (`PUSH_GUARD`) into the hooks directory the engine's environment pins (`sandbox.engine_git_env`), calls `Workspace.push`, and removes the hook. The hook refuses any commit but the scanned one. When `ship.sh` then fails and the head is another, `_push` returns `False`: `_step_land` journals `push_withheld` with both heads and sends the issue to `verify`; `_revert` fails with `main_broken`, as it did after the fact. Pushes take one lock in a run (`_push_lock`) and, across runs, an flock on the hooks directory itself (`_hooks_held`), from before the hook is written until it is removed, whether the push is scanned or not: every run of a repository is given the same hooks directory, so without it a second run replaced or removed the hook of a push under way, and git published a head no scan had seen. Where git's hooks directory is not one the engine's environment names, a scan is not run and the push is withheld. With no list, `_push` removes a hook a stopped run left and pushes as before. | `ship.sh` fetches `main` again and merges it or rebases onto it before it pushes: with a `main` that moved after the scan, it would publish a head and commits the owner's list was never held against. `Workspace.push` takes no environment and `workspace.py` is not named by the issue, so the guard is a file in the directory the engine already owns. `sandbox.py` and the README of this directory call that directory empty; it is, except while the engine pushes a scanned head. `ship.sh` tries a refused push four times, about 14 seconds in all, before it gives up. |
| `implement_loop/engine.py` | `LOCAL_VALUES_ENV`, `local_value_checks` and `local_values_file` are imported inside a `try`, with stand-ins that name no list and no scan. | `tests/test_unwritable_probe.py` loads the `engine.py` beside it into the `implement_loop` of the engine that runs the loop (see the entry of the deployment verdict above). While this patch is built, that engine's `config.py` has none of the three. Once this patch runs the loop, the `except` branch is dead. |
| `tools/hygiene/README.md` (outside this directory) | The paragraph that said the loop's pushes are not checked against the owner's list now says where they are, and "Where it runs" has a row for it. | The gap it described is closed. |

Behaviour an upstream reader would not expect: the engine starts a process outside the sandbox
that is neither git nor the ship script; an issue can end `failed` with the cause `history` on a
branch every check in the sandbox passed; a `history_refused` event can carry findings without a
line number and no output; a push can be withheld because `main` moved, with the issue back in
`verify` and nothing published. Three things are left as they were. `sandbox.clean_env` passes
`HYGIENE_LOCAL_VALUES` into the sandbox, where the file it names does not exist and the scanner
then fails: an owner who sets that variable, instead of using the default file, fails every
standing check. The verifier, the hooks and CI are unchanged. And a value already on `main` that
the owner adds to the list later is found in the tree of every head, so every issue then ends
`failed` until `main` is corrected.

### 1.2.1, patched a fifth time: a change to a workflow file is landed by the owner (2026-11-14)

The factory's GitHub identity has no workflows permission, so that the factory cannot change its
own gates (ADR 0001, amendment 2), and GitHub refuses a push whose commits change a file under
`.github/workflows/`. Until now the engine met that refusal at the land step, as a `GitError` it
filed as an engine fault. Issue #17 (draft E0-23) gives such a change an outcome: it is built,
verified and certified as any other, the engine pushes nothing of it, the owner pushes the
certified head, the engine opens the pull request, and the owner merges it. The rule does not
depend on the identity the engine runs as. The engine's version stays 1.2.1: nothing here repairs
a failed issue, and the wait this patch adds resumes on any run.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/model.py` | New wait kind `WAIT_OWNER_LANDING` (`owner_landing`). `IssueState` gains `owner_landing`: the branch and the certified head the owner is given, with the workflow paths, the head whose comment was posted (`announced`), the head whose pull request was opened (`published`, `pr`), the head it takes the place of (`replaces`, `conflict`) and, for a revert, `revert`, `of` and `feedback`. | The issue's first deliverable: the kind is defined here, and the branch and the certified head are recorded. A state written before this patch reads with the field empty. |
| `implement_loop/engine.py` | `_step_land`, after the scans of the head and before `_push`: `_workflow_paths` lists the paths under `.github/workflows/` (`WORKFLOWS`) that the commits `main..head` change. Every commit is read by itself (`git log --name-only -c --no-renames`), a merge for what differs from every parent, and the whole difference with `main` is added. If there is one, `_owner_lands` records the branch and the head, posts the comment (`_hand_over`) and makes the issue wait (`WAIT_OWNER_LANDING`, resumed in `land`); nothing is pushed, and the step's end releases the merge queue with no deployment record written. | The issue's first deliverable. A commit that changes a workflow file and a later one that puts it back are both published, so both count. A merge of a `main` that changed a workflow file publishes nothing of the branch's own, so it does not count: every branch would otherwise become the owner's after the owner lands one such change. |
| `implement_loop/engine.py` | `_hand_over`: one comment on the issue per certified head, with a marker that carries the head (`OWNER_LANDING_MARKER`), so a step that ends between the comment and its record writes no second one. It names the branch and the full head, gives `git push <remote> <head>:refs/heads/<branch>`, says that the loop opens the pull request and the owner merges it, and asks the owner not to use GitHub's button that brings the branch up to date. A comment GitHub did not take is posted by the next resumed step. | The second deliverable. The command names the commit and not the local branch, so it pushes the certified head whatever the local branch is at, and git refuses it unless it fast-forwards the remote branch. |
| `implement_loop/engine.py` | A land step resumed with the record (`_maybe_resume` resumes the new kind on every run) never calls `Workspace.update`. `_owner_pull` reads the remote branch with `git ls-remote` (`_remote_head`): no such branch, or another head, and the issue waits again with a detail that says which, and no pull request is opened. With the certified head there, `_open_pull` opens the pull request and writes the evidence comment, once for a head, and the issue waits again. `_open_pull` is the code `_step_land` had inline for a branch it pushed, moved into one method that both paths call. A pull request the owner closed without a merge is not opened again. `merge_pr` is not called on this path. | The third and fourth deliverables. One method for both paths is what makes the body and the evidence the same. |
| `implement_loop/engine.py` | `_conflicts_with_main`: for an issue with the record, whether the certified head still merges into `main`. Nothing to do when the head contains `main`; an open pull request at that head whose record says `mergeable: true` is believed; otherwise `git merge --no-commit --no-ff` is tried in the issue's worktree and always undone. On a conflict the existing `_conflict` takes over: a build round with `main`'s merge in progress, the verifier, the examiner. Back in `land` with a head other than the recorded one, the new head is scanned like any head, recorded in place of the old one, and posted in a new comment that names both. Without a conflict nothing is merged, verified or examined, and the scans of the head are not run again. | The fifth and sixth deliverables. `GhCli.find_pr` reads the list of pull requests, which carries no `mergeable`, and `github.py` is not a file the issue names: so with the real client the merge tried in the worktree decides, also once the pull request is open. The new head descends from the old one because the engine only ever adds commits to a branch. |
| `implement_loop/engine.py` | `_merged` empties the record, and `_new_round` does too. The pull request found merged at the top of `_step_land` is handled as before (`merge_found_on_resume`, `_delivery`), so the post-merge verification, the evidence and the deployment verdict follow as for any issue. | The sixth deliverable: from `merged` on, nothing is different. |
| `implement_loop/engine.py` | `_revert`, before its push: the same `_workflow_paths` on the revert's commits. If the revert changes a workflow file, it is recorded (`revert`, the pull requests it undoes, the feedback of the round that follows it), and `_owner_revert` posts the comment and makes the issue wait (`WAIT_OWNER_LANDING`, resumed in `merged`); the revert is not pushed and not merged. `_step_accept` sends an issue with such a record to `_owner_revert` before anything else: the remote branch at the certified head opens the revert's pull request (`_revert_pull`, the body `_revert` had inline); that pull request merged with that head, or one that descends from it, starts the next round (`_reverted`, the end `_revert` had inline); a `main` that moved so that the revert no longer merges ends the issue `failed` with the cause `main_broken`. `_delivery` takes the reviewed head as an optional argument for it. `_owner_edit` drops a revert's record. | The examiner's finding on the plan. A workflow change the owner merged can break `main` or its deployment, and after the fix-forward `_revert` pushed and merged by itself, outside the land step: under the owner's login that push would succeed. A revert has no builder to resolve a conflict, and `_revert` already ends in `main_broken` when `main` moves under it. |
| `implement_loop/engine.py` | `WAIT_OWNER_LANDING` is imported inside a `try`, with the same value as a stand-in, and the record is read with `getattr` (`_owner_record`). The wait's hint for the progress comment is added to `report.WAIT_HINT` from here. | `tests/test_unwritable_probe.py` loads the `engine.py` beside it into the `implement_loop` of the engine that runs the loop (see the entry of the deployment verdict above), whose `model.py` has neither the kind nor the field while this patch is built. `report.py` is not a file the issue names. |

Behaviour an upstream reader would not expect: an issue can be certified and never pushed by the
engine; a pull request can be opened by the engine on a branch it did not push, and merged by
someone else; the land step can leave a branch behind `main` for as long as the two merge; a
revert can wait for a person. Left as they were: `github.py`, so GitHub's own `mergeable` is read
only where a pull request's record happens to carry it; the supervisor, which counts an issue that
waits for the owner as unsettled and stops at its bound of runs, as it does for an approval in the
pipeline; and a remote branch the owner brought up to date with GitHub's button, which is another
head than the certified one: the issue waits, says so, and the engine certifies nothing GitHub
made.

### 1.2.1, patched a sixth time: a merge refused for want of the owner's review (2026-11-14)

With the owner's yes to decision 8 of the design, a pull request that touches a protected path
needs the owner's approving review before GitHub merges it (system design, section 8.4, control 5;
ADR 0001, amendment 3). The amendment says the engine already has an outcome for such an issue. It
did not: the land step counted the refused merge as a merge refusal, sent the issue back to
verification, and after three refusals made it wait as if GitHub were failing (`github`). Issue
#19 (draft E0-25) gives that case its own wait. The head is certified and the required check is
green on it, so nothing is verified again, and the landing claim is released while the owner
reviews. The engine's version stays 1.2.1: nothing here repairs a failed issue, and the wait this
patch adds is read again by every run.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/github.py` | New in the `GitHub` protocol, `GhCli` and `FakeGitHub`: `review_decision(n)`, the review decision of pull request `n`. `GhCli` reads the field `reviewDecision` of GitHub's GraphQL API with `gh api graphql` (`REVIEW_DECISION_QUERY`; the query as a raw field, the owner and the name as raw fields, the number as a typed one) and returns `APPROVED`, `CHANGES_REQUESTED`, `REVIEW_REQUIRED`, or an empty string where GitHub gives null. `_api` takes `fields`, the field flags of that one call, so the query is retried like every read. `REVIEW_OWED` names the two decisions that owe a review. `FakeGitHub` keeps a decision on each pull request (`review_decision`, set from `default_review` when it is opened; empty by default) and `merge_pr` refuses while it is one of `REVIEW_OWED`. | The first deliverable. The REST API has no such field. The words of GitHub's refusal are not a contract, the review decision is. With the default, the fake merges as it always did, so no earlier test changes. |
| `implement_loop/model.py` | New wait kind `WAIT_OWNER_REVIEW` (`owner_review`). | The second deliverable: the kind is defined here. The wait itself records the pull request and the head (`pr`, `head`); `IssueState` is unchanged. |
| `implement_loop/engine.py` | `_step_land`, where `merge_pr` raised and the pull request is not found merged after all: the review decision is read (`_review_decision`) before anything is counted. With `REVIEW_REQUIRED` or `CHANGES_REQUESTED`, on a pull request whose head is still the certified one, `_owner_reviews` journals `owner_review_owed`, says so on the pull request and makes the issue wait (`WAIT_OWNER_REVIEW`, resumed in `land`): `merge_refusals` is not incremented, no `merge_refused` is journaled, the issue does not go back to `verify`, and the step's end releases the merge queue as it always does. With any other decision (`APPROVED`, none, one that could not be read), or on a pull request whose head GitHub gives as another one, the lines that follow are the ones that were there: a counted refusal, back to `verify`, the wait `github` after three. | The second, third and fifth deliverables. A pull request with another head was refused for that head, whatever its review decision says, and waiting for a review of it would wait for ever. |
| `implement_loop/engine.py` | `_say_review_owed`: one comment on the pull request per certified head, found again by a marker that carries the head (`OWNER_REVIEW_MARKER`). It gives the head and the review decision and says that nothing is verified meanwhile, that the loop merges this head once the pull request is approved, and that a push makes another head. The words are the engine's own; nothing of GitHub's refusal is repeated. | The third deliverable: said once. A head certified later (after `main` moved) is another head to review, so it gets its own comment. |
| `implement_loop/engine.py` | `_maybe_resume` resumes the new kind only when `_review_owed` says the wait is over. It reads the pull request and its review decision, with no step and no claim. Still open at the head of the wait with any decision but `APPROVED` (a review owed, or none at all), or GitHub could not be read: the issue stays as it is, and with a review owed a comment GitHub did not take before is written. `APPROVED`: the issue resumes in `land`, whose unchanged step merges the certified head. Merged or closed, or with another head: it resumes in `land` too, which reads that as it always did. | The fourth deliverable. Resuming into the land step at every run, as the other owner waits do, would take the merge queue, scan the head again and ask GitHub for a merge it is known to refuse. A `main` that moved during the wait is merged by the land step once the review is given, and the new head is verified and certified again before it lands, as for every landing. Only `APPROVED` releases an unchanged head: the issue says the engine keeps waiting while the decision is not that one, and an empty decision on a head GitHub has just refused is no approval. An owner who lifts the review rule instead approves or merges the pull request, and either ends the wait. |
| `implement_loop/engine.py` | `WAIT_OWNER_REVIEW` and `REVIEW_OWED` are imported inside a `try`, with the same values as stand-ins, and the client's `review_decision` is looked up with `getattr`. The wait's hint for the progress comment is added to `report.WAIT_HINT` from here. | As for the waits before it: `tests/test_unwritable_probe.py` loads the `engine.py` beside it into the `implement_loop` of the engine that runs the loop, whose `model.py` and `github.py` have neither while this patch is built. `report.py` is not a file the issue names. |

Behaviour an upstream reader would not expect: a refused merge is not always counted; the engine
asks GitHub's GraphQL API a question; an issue can wait at the start of a run without any step
being run for it. Left as they were: the supervisor, which counts an issue that waits for the
owner's review as unsettled and stops at its bound of runs, as it does for the other waits for the
owner; a remote branch that someone else pushed to, which `ship.sh` refuses to push over, as
before; and whether an approval survives a new head, which is the ruleset's choice (issue #20):
where it does not, the new head is refused, and the issue waits again with a comment for that
head. What only the real GitHub can show (that it refuses the merge under the ruleset and the
`CODEOWNERS` entry of issue #20, and gives `REVIEW_REQUIRED` then) is not shown by these tests,
which run against the fake.

### 1.2.1, patched a seventh time: the reporter's facts for a fix-forward (2026-11-14)

A failed deployment went down the broken-main path with feedback that named the failed context and
its state, and nothing else (the patch "the deployment verdict" above). That does not tell a
builder what to fix. The pipeline's reporter adds a structured report to a failure, as further
commit statuses on the same commit, one per fact, each under the context of its stage followed by
a slash and a suffix (system design, section 8.4; ADR 0001, amendment 4). Issue #23 (draft E0-29)
gives the fix-forward a summary built from those statuses. The engine's version stays 1.2.1:
nothing here repairs a failed issue.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/github.py` | New: `commit_statuses(sha)` on the `GitHub` protocol, on `GhCli` and on `FakeGitHub`: every commit status of the commit, the oldest first, each as its context, its state and its description, and no other field. `GhCli` reads every page of `commits/<sha>/statuses` and orders them by id. `FakeGitHub` returns what `set_status` recorded. `RedactingGitHub` and `RecordingGitHub` forward the call like any other read. `deploy_status` is unchanged. `FakeGitHub` also gains `commit_comments` and `add_commit_comment(sha, body)`: a comment the fake holds and no method of the protocol reads. | The first deliverable. The facts of a failure are statuses of contexts the engine cannot list ahead, so the commit's statuses are read whole. The fake's commit comment is there so that a test can show that nothing of one reaches a fix-forward; there is no read of commit comments, in the protocol or in `GhCli`. |
| `implement_loop/engine.py` | `_deployment_failed` adds the entries of the new `_deployment_summary` to the fix-forward's feedback, after the line it gave before (the commit, the failed configured contexts, their states), which is unchanged. They are built from `commit_statuses` of the merge commit and from nothing else. Only the statuses whose context is a configured context of `[deploy]`, or one followed by a slash and more, count. The first kind is a stage's own status, whatever its state (a stage that succeeded is told too). The second kind is a fact of the reporter. Each such context gets one feedback entry: its context, its state and its description (white space folded to single spaces, left out when empty), from the newest status of that context. The entries come in the configured order of the stages, each stage's own status before its facts, the facts sorted by context. A status of any other context is left out: `measure/...`, a context that is not configured, one that only starts with the same characters (`deploy/beta-canary` beside `deploy/beta`). | The second deliverable. The slash keeps a sibling context from passing for a fact. The newest status of a context is the one that counts, as for the verdict. The description of a configured context's own status is in the feedback, which the earlier patch ("the deployment verdict") kept out: the issue asks for the contexts, states and descriptions of the configured contexts as of those below them, and one assertion of `tests/test_deploying.py` follows (below). |
| `implement_loop/engine.py` | `_deployment_failed` reads the statuses before it journals `deployment_failed`, releases the landing claim or starts the fix-forward. If GitHub does not answer, the issue waits with the kind `github` and resumes in `deploying`, with the landing claim still held, as when the verdict itself cannot be read. | Each issue has one fix-forward before a revert: it is not spent on feedback without the facts. The next run reads the same commit, finds the failure again and reads the facts. |

Behaviour an upstream reader would not expect: the feedback of a fix-forward, and with it the body
of a revert pull request and a failure's reason, which repeat that feedback, carry the
descriptions of commit statuses, which the engine did not write; they pass through the redacting
client like everything published. Whoever can post a commit status below a configured context can
put a line in a builder's feedback: on GitHub that is the reporter's App and the factory's own
identity (system design, section 8.4). The event `deployment_failed` and the evidence record of a
deployment keep their shape. A failed deployment makes one more read of the commit's statuses than
before.

## Tests removed or adapted

### 1.1.0

Upstream has 389 collected tests at the recorded commit (388 passed, 1 skipped there). After the
port there are 366: 29 removed with what they tested, 6 added.

| Test | Change | Why |
| --- | --- | --- |
| `test_environment.py`: `ProbeTests` (6 of 9): `test_missing_node_modules_fails_the_node_probe`, `test_packages_missing_or_not_as_locked_fail_the_node_probe`, `test_the_doctor_reports_the_shared_library_error`, `test_the_doctor_sees_the_active_copy_through_its_environment`, `test_ldd_names_missing_libraries_the_doctor_message_hides`, `test_a_missing_browser_binary_is_recognised` | Removed. | The Node and browser probes are removed. |
| `test_environment.py`: `HealTests` (11 of 13): every test of the staged `npm ci`, the browser install and the library recipe | Removed. | Those repairs are removed. |
| `test_environment.py`: `BrowserEnvTests` (3) | Removed. | `browser_env` is removed. |
| `test_environment.py`: `RecipeTests` (8, one of them the test upstream skipped) | Removed. | They test upstream's `setup/browser-libs.py`, which is not vendored. |
| `test_scheduler_state_verify.py`: `ProvisionTests` (1) | Removed. | `Workspace.provision` is removed. |
| `test_environment.py`: `ProbeTests.test_a_healthy_host_passes_each_kind`, `test_failures_are_parsed_per_probe`; `HealTests.test_a_healthy_host_needs_nothing`, `test_a_missing_uv_is_reported`; the fake host | Adapted: they assert on the one probe that remains, with the same expectations for it. | Same. |
| `test_environment.py`: `ProbeTests.test_the_pinned_runner_is_the_one_ci_uses` | Adapted: the pin is looked up in `pyproject.toml` and `scripts/check.sh` instead of upstream's `engine-tests.yml` workflow. | That is where this repository pins pytest for CI. |
| `test_environment.py`: `FingerprintTests` | Adapted: the probe names in its data. The assertions are the same. | The old names were the removed probes. |
| `test_environment.py`: `DoctorRepositoryTests` (5) | Added. | The doctor's two additions: no remote, a remote nothing was pushed to, a pushed `main`, an unreadable remote. |
| `test_parsing_and_graph.py`: `test_branch_and_session_relative_paths` | Renamed `test_branch_and_repo_relative_paths`; it now expects the path as the issue writes it, and checks that the recording's `session:01` label no longer moves it. | Session-relative paths are removed. |
| `test_parsing_and_graph.py`: `test_epic_10_plan_matches_the_filed_waves` | Adapted: the plan is built against a temporary directory that holds the four local files the recorded issues reference, instead of this repository. Every assertion is kept, `warnings == []` included. | The recording's references exist upstream, not here. |
| `test_parsing_and_graph.py`: `test_a_reference_that_does_not_exist_is_a_warning` | Added. | The other half of the adapted test: without those files, exactly one warning per reference. |
| `test_parsing_and_graph.py`: `test_path_tokens_exclude_commands_and_numbers` | One example path changed. | It named an upstream tool. |
| `test_live.py` | `parse(issue, [])` became `parse(issue)` in the fixture. | The parameter is removed. |
| `test_verify_accounting.py`, `test_probe_command_retirement.py`, `test_probe_finding_closure.py`, `test_scheduler_state_verify.py` | The directory name in path literals: `sessions/` became `packages/`. No assertion changed. `test_shared_files_serialize_but_session_readmes_do_not` is renamed `…_but_nested_readmes_do_not`. | The tests use it as an arbitrary subdirectory; the name is upstream's. |
| `test_engine_recovery.py` | One comment reworded. | It named upstream's browser. |
| `test_environment.py`: the `WSL1` constant | Split over three lines, same value, with an inline `hygiene: allow email` marker. | The WSL1 kernel banner contains text shaped like an address; `tools/hygiene/scan.py` would refuse it. |
| `tests/helpers.py` | `present` uses a type parameter (`def present[T]`) instead of a module-level `TypeVar`. | Ruff's `UP047` at this repository's target, Python 3.12. The tests therefore need Python 3.12; the engine itself still runs on 3.11. |

### 1.2.0

529 collected tests: the 366 of the port, none removed and none changed (only the helper module
they share), and 163 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/helpers.py` | Adapted. It installs `sandbox.PassThrough()` for the whole suite and sets `config.STANDING_CHECKS` and `config.HISTORY_CHECKS` to `()`, keeping the engine's own values under those names for the tests that turn them on. | The existing tests drive fake agents and real check commands in temporary repositories: nothing to isolate, no scanner, and in CI maybe no bubblewrap. Every launch still goes through the engine's sandbox hook. This is the only change that reaches an upstream test. |
| `tests/test_sandbox.py` (82) | Added. | The command line and the allow-list; the scrub (names, letter case, shapes, a vendor's own key); what a worktree may see, and that an agent's `.git` file or `alternates` cannot widen it; that each of the four launch sites asks the sandbox and that nothing starts when it refuses; the engine's own git running no hook an agent left; the doctor's lines and `--canary`; `run` and `supervise` refusing alike. With a real bubblewrap (skipped without one, which fails CI): the home directory really empty, the credentials unreadable, only the worktree writable, timeouts and stops killing everything, the canary passing here and failing on a loose allow-list, an account's home hidden when `$HOME` points elsewhere. |
| `tests/test_standing_checks.py` (18) | Added. | With this repository's real scanner: a clean head passes and the scan is recorded first; a leak is a problem that carries the redacted output; a tree without the scanner fails closed; a round the scan refuses is never committed, examined or pushed, and the builder can fix it; refused work is not checkpointed; a revert is scanned before it is pushed. |
| `tests/test_history_checks.py` (18) | Added. | A value that reached a commit ends the issue `failed` without another round, on a branch that is never pushed; a value committed and then removed, in a head that was certified, is stopped before the push; a retry builds on a new branch and lands; a check that cannot run is a failed check and never an abandonment, and cannot go round for ever; a revert's commits are scanned; a revert finds the merge that broke `main` past an abandoned round; a probe the scan refuses is not adopted, and the examiner is told why. |
| `tests/test_dollar_ceilings.py` (14) | Added. | `--max-budget-usd` per role; a call that stops at its budget is charged and not retried; costs add up per issue; the ceiling fails the issue like the call ceiling; retries, revivals and owner edits. |
| `tests/test_protected_zones.py` (19) | Added. | A protected zone the issue does not name is withheld, one it names is granted; naming a wider directory names nothing protected inside it; the agents' configuration (`.claude/`, `.agents/`, `.codex/`) is protected like the gates: a plan cannot reach it by listing it, an issue can name a path in each directory, and a typed request is the other way in; a repair run never gets the safety surface; a typed request the examiner approves is the other way in. |
| `tests/test_commit_identity.py` (12) | Added. | The doctor's `commit identity` line (allowed, refused and never shown, overridden by the environment, a refused name, the allow-list read from the repository's own file, no scanner) and its `git config` line. |

Two things about how they are written. The tests named `Real…` in `test_sandbox.py` need a
bubblewrap that may create namespaces; `.github/workflows/ci.yml` prepares one. Values the hygiene
scanner would refuse (an account ID, a token, an address) are put together at run time, so that
no line of a test file holds one.

The `Real…` tests are hermetic about the host they run on. A host that names its own configuration
directory (`XDG_CONFIG_HOME` or `GH_CONFIG_DIR`, as GitHub's runners do) would point the canary at
its real `gh` login instead of the fake home's, so the tests unset both. The variables the host
itself sets under `GH_` or `GITHUB_` that the scrub keeps by design (a runner's `GITHUB_ACTION`,
`GITHUB_REPOSITORY`, ...) are expected inside, and the planted credentials are still asserted
absent. `/run` inside holds the path to `resolv.conf` when the host keeps it there (Ubuntu's
systemd-resolved), which the sandbox binds back on purpose, and nothing else. No assertion about
what the sandbox hides or drops was removed.

### 1.2.0, patched (2026-11-14)

532 collected tests: the 529 of 1.2.0, none removed and none changed, and 3 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_protected_zones.py`: `PlanGrantTests` (3 more, 22 in the file) | Added: `test_the_pipeline_stack_and_the_bootstrap_policies_are_protected`, `test_a_plan_zone_there_is_withheld_unless_the_issue_names_it`, `test_neither_folder_is_on_the_safety_surface`. | A file under `src/rhi/pipeline/` or `setup/bootstrap/` is protected and the files beside the two folders are not; a plan that lists one gets it only when the issue's deliverables name it, or the folder above it; neither folder is on the safety surface, so a repair run whose issue names them is granted them. The three read the four lists from the `implement_loop/config.py` beside them (`judge_by_this_trees_lists`): under the verifier, the plugin of the running engine has already imported that engine's `implement_loop`, and `loop_pytest.py` drops it again only in the pytest that records, not in the xdist workers of `scripts/check.sh engine-tests`, where the tests would otherwise judge the running engine's lists. `loop_pytest.py` is on the safety surface and is not changed here. |

### 1.2.1

535 collected tests: the 532 before it, none removed and none changed, and 3 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_unwritable_probe.py`: `UnwritableProbeTests` (3) | Added: `test_the_file_system_refuses_the_probes_path`, `test_the_lint_refuses_it_instead_of_raising`, `test_an_examination_that_names_one_carries_on`. | The regression of fault a25bef17ec: a probe whose file name is longer than a file system takes. The first shows that the host refuses the path, the second that the lint answers with a reason, the third that an examination whose blocking finding names such a probe rejects it, adopts the probe beside it and still delivers the issue. They take `Engine` from the `engine.py` beside them (`engine_beside_these_tests`), for the reason `judge_by_this_trees_lists` gives above: in an xdist worker under the verifier, `implement_loop` is the running engine's, which is the one with the fault while its repair is built. `loop_pytest.py` is on the safety surface and is not changed here. |

### 1.2.1, patched (2026-11-14)

545 collected tests: the 535 before it, none removed and none changed, and 10 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_redaction.py` (10) | Added. | With this repository's real scanner, and an account number and an ARN put together at run time. `redact` asks the scanner beside the engine and raises when it is missing or fails; the client masks the five calls, sends nothing it could not mask and forwards a tick as written; the client `cmd_run` builds masks what it sends. In real engine runs behind the redacting client: the two values reach the fake GitHub masked in an evidence comment, a pull request body, a fault issue and a follow-up issue; with the scanner removed nothing is published, the issue waits with the kind `github`, and the next run resumes it; a scanner in the issue's worktree that masks nothing is never run; a failure on the evidence comment, the follow-up issue, the comment about deferred items or the last progress comment leaves the issue open and waiting, and the follow-up is filed once; a progress comment that cannot be masked stops the issue when its step ends; an issue the owner ticked is not closed until its last comment is written, and waits, so that the supervisor comes back to it; a failed issue whose fault issue or last comment could not be masked waits as it failed, and is failed again, unworked, once the next run has filed the fault. The tests take the engine from the `implement_loop` beside them (`loop_beside_these_tests`), for the reason `judge_by_this_trees_lists` gives above: in an xdist worker under the verifier, `implement_loop` is the running engine's, which has no `redact.py` while this change is built. `loop_pytest.py` is on the safety surface and is not changed here. |

### 1.2.1, patched again (2026-11-14)

561 collected tests: the 545 before it, none removed and none changed, and 16 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_deploying.py` (16) | Added. | `deploy_status` returns the newest status of a context, for `GhCli` behind a fake `gh` and for the fake GitHub alike; the committed `loop.toml` loads to the three neutral values, and a key the table does not have is refused. In real engine runs, with the fake GitHub posting statuses on merge commits as a pipeline would: an empty contexts list reads nothing and waits for nothing; success of every context is journaled between the merge and the evidence step and recorded as evidence with the commit and the contexts; a failure gets one fix-forward whose feedback names the context and its state and not the status's description; an error that the fix-forward does not repair ends in a revert pull request; a second issue stays in `land` while the first polls, and does not land after the timeout either, until the first has its verdict (`test_the_landing_claim_is_held`); a run for another root does not land before that verdict; an approval leaves the issue waiting `owner_approval` with the claim free, a second issue lands meanwhile, and the first ends done once its status is success (`test_an_approval_releases_the_claim`); that wait has no timeout, a failure ends it, and an empty `approval_description` matches no status; a resumed wait reads the same commit and finds one merge (`test_a_resumed_wait_does_not_merge_again`); statuses that cannot be read are no verdict and no engine fault; the evidence step is not reached with a verdict owed; emptying the contexts frees the issue and the claim; a run stopped while it polls renders the phase in its summary and resumes in `deploying`. The module takes `implement_loop` from the folder beside it (`loop_beside_these_tests`), for the reason `judge_by_this_trees_lists` gives above. |

### 1.2.1, patched a third time (2026-11-14)

571 collected tests: the 561 before it, none removed and none changed, and 10 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_container_obligations.py` (10) | Added. | The committed `loop.toml` maps `host:container-runtime` to `container-tests` and still lists the capability. In real engine runs, with the fake GitHub playing the job's check run (`CI`, in the test module: `FakeGitHub` answers every name with the required check's verdict, so the subclass answers by name, and `github.py` is not changed): a success is read on the merged head, ticks the item with the check run's address, records it as evidence and leaves only the other obligation listed; an absent, a skipped and a failed check run leave the obligation the owner's on a `delivered` issue; so does an item that names no test under `tests/container/`, for which nothing is read; an item that needs `aws:deployed-environment` stays the owner's whatever the check run says (`test_other_obligations_are_untouched`); an issue whose only obligation was discharged ends `done` and closes with its epic (`test_an_issue_with_nothing_left_is_done`). The modules come from `test_deploying.py`'s `loop_beside_these_tests`, for the reason given there. |

### 1.2.1, patched a fourth time (2026-11-14)

582 collected tests: the 571 before it, none removed and none weakened, and 11 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_history_checks.py`: `HistoryCase.setUp` | Three lines added: `XDG_CONFIG_HOME` names an empty directory and `HYGIENE_LOCAL_VALUES` is unset, for the test. | The tests of this module run with the history checks on. On a machine that holds the owner's list they would otherwise scan with it. No assertion of an earlier test changed. |
| `tests/test_history_checks.py` (11) | Added: `TheScanWithTheOwnersListTests` (3), `OwnersListTests` (7), `OwnersListRevertTests` (1). | The commands and where the list is looked for; what is kept of a finding. In real engine runs, with a list written by the test, a value put together at run time, and this repository's scanner: the value in the tree of the head, or only in an earlier commit, ends the issue `failed` with the cause `history`, nothing pushed, and a retry lands on a new branch; the value, the words of its line and the scanner's excerpt are in nothing the fake GitHub received, no agent was given them, and no file of the run's directory holds them, while the path and the rule's name are in the event, the failure, the fault issue and the next builder's feedback; a branch whose own `scan.py` accepts everything is refused all the same, and that copy ran in the sandbox only (`Inside`, a launcher that hides the list as the sandbox does and marks what it starts); with no list no process is started outside the sandbox and the push goes ahead; a list that cannot be read, and a git whose hooks directory is not the engine's, withhold the push; a head `ship.sh` made after the scan, because `main` moved, is not pushed before it is scanned; a revert whose commit message holds a value of the list is not pushed. The engine runs take `implement_loop` from the folder beside the tests (`L`, from `test_deploying.py`'s `loop_beside_these_tests`), for the reason given there. |

### 1.2.1, patched a fifth time (2026-11-14)

590 collected tests: the 582 before it, none removed and none changed, and 8 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_owner_landing.py` (8) | Added. | Real engine runs on a bare remote whose `main` has `.github/workflows/ci.yml`, with the fake agents and the fake GitHub. The test plays the owner: it runs the push command of the engine's comment, as written, in the repository the engine runs in, and merges a pull request with a squash in the remote and not through the fake's `merge_pr`, so every `merge_pr` in the fake's log is the engine's. A branch that changes the workflow file: nothing on the remote, the issue waiting `owner_landing` with its record and one comment, the merge queue free, no fault. The remote branch at the certified head: one pull request, with the body and the evidence the renderers give, no merge call, and `done` after the owner's merge, with the post-merge verification and the deployment verdict. Another head on the remote: no pull request. A conflicting move of `main`: one build round, a new verification and examination, a head that descends from the old one and holds `main`, a second comment, the remote branch untouched until the owner's fast-forward push, then `done`. A clean move of `main`: no verification, examination or round, the worktree at its head. A change to another file under `.github/`, and a branch that merged a `main` whose workflow file changed: pushed and merged by the engine. A workflow change whose deployment fails twice: the revert certified, not pushed, not merged, handed over, its pull request opened after the owner's push, and the next round the owner's to land again. |

### 1.2.1, patched a sixth time (2026-11-14)

595 collected tests: the 590 before it, none removed and none changed, and 5 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_owner_review.py` (5) | Added. | `GhCli.review_decision` against a scripted `gh`: the call is `gh api graphql`, its query names `reviewDecision`, a value and a null are read. In real engine runs, with the fake agents and the fake GitHub: a merge refused with `REVIEW_REQUIRED`, and with `CHANGES_REQUESTED`, leaves the issue waiting `owner_review` after one refusal, with none counted, nothing verified after it and one comment on the pull request, while a second issue of the same run, built once the first waits, lands (the merge queue was free); a resumed run with the review still owed runs no step. Once the fake's decision is `APPROVED`, a resumed run merges the head certified before the wait, with no verification before the merge, and the issue ends `done` (`test_a_given_review_merges_the_certified_head`). A refusal with `APPROVED`, with no decision and with one that cannot be read is counted three times and ends in the wait `github` (`test_other_refusals_are_unchanged`). A `main` that moved during the wait is merged and verified again before the landing, and a pull request that got another head is refused and verified as before. The modules come from `test_deploying.py`'s `loop_beside_these_tests`, for the reason given there. |

### 1.2.1, patched a seventh time (2026-11-14)

603 collected tests: the 595 before it, none removed, one assertion of one test turned around, and 8 added.

One probe outside the engine's tests is adapted with them (the last row).

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_deploy_summary.py` (4) | Added. | `commit_statuses` gives every status of a commit, the oldest first, with the three fields and no other, for `GhCli` behind a fake `gh` and for the fake GitHub alike. In real engine runs with the fake agents and the fake GitHub, which holds the statuses a reporter would post on the merge commit: the fix-forward's feedback keeps the verdict line, names a failed resource with its status code and a failed smoke test, takes the newest status of a context, keeps the configured order of the stages, and no agent is told anything of a commit comment the fake holds (`test_the_summary_comes_from_statuses_only`); statuses under `measure/`, of a context that is not configured and below it, and of a context that only starts like the configured one are not in it, while a fact below the configured context is (`test_other_contexts_are_left_out`); statuses that cannot be read for the facts leave the issue waiting `github` with no fix-forward spent, and the next run gives the fix-forward its facts. That a stage's own status is in the feedback with its description is asserted by the two rows below. The modules and the loop come from `test_deploying.py`, for the reason given there. |
| `tests/test_examiner_deploy_summary_probe.py` (4) | Added, by the examiner. | The examiner's probes of the same patch, adopted as tests: `commit_statuses` across two pages; the statuses of the configured contexts themselves, a failed one and a successful one, are in the feedback with their states and descriptions; a failed resource and a failed smoke test reach the builder and a commit comment does not; statuses under `measure/`, of contexts that are not configured, of a sibling context and of another commit are left out. |
| `tests/test_deploying.py::test_a_failed_deployment_gets_a_fix_forward_that_names_the_context_and_its_state` | Adapted: one assertion turned around. | It asserted that the description of the failed context's status is not in the fix-forward's feedback, which was the behaviour of the patch "the deployment verdict". Issue #23 puts the description of a configured context's status in the summary, so the test now asserts that it is there. Its other assertions are unchanged: the context, its state, the commit, one fix-forward, two merges, the evidence of the second. |
| `.examiner-probes/test_deploy_failure.py::test_final_bad_status_is_sent_to_fix_forward` (at the root of the repository, outside the engine's tests) | Adapted: the same assertion turned around. | The examiner's probe of issue #12, adopted with the patch "the deployment verdict" and run again on `main` after every later merge of a run. It asserted, for a `failure` and for an `error`, what the test above asserted: that the description of the failed context's status is not in the fix-forward's feedback. The first delivery of issue #23 turned the assertion around in `tests/test_deploying.py` and left this copy as it was, so issue #12's checks failed on `main` once the patch had merged. It now asserts that the description is there. Its other assertions are unchanged: the context, its state, one fix-forward, two merges, the issue done. |

## Kept as recorded

`tests/fixtures/epic10.json` is a recording of GitHub's answers for epic #10 of the upstream
repository, made with `implement record-fixture`. It is test input for the graph and the parser
and is not edited: its issues talk about sessions, HTML pages and upstream paths, and it carries
many external links. Several tests also keep upstream's capability names (`hardware:laptop`,
`human:rehearsal`) and example file names as arbitrary test data.
