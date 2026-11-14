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
| Engine version here | 1.2.0 |
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
`[deploy]` table, no `deploying` phase. Nothing of it is in the engine. It is backlog issue #12
(draft E0-07).

### The `costs` command (2026-11-14, backlog issue #11)

The report the dollar ceilings and the per-role budgets are set from (ADR 0001, decision 8 and
Consequences). The engine version stays 1.2.0: the command only reads, and a new version would
revive every failed issue.

| File | What changed | Why |
| --- | --- | --- |
| `implement_loop/cli.py` | New command `costs <issue>` (`cmd_costs`, `cost_report`, `render_costs`), reached as `scripts/implement.sh costs <issue>`, with `--json` for the same figures as data. It reads the run's `agents.jsonl` and, since the run records do not keep an issue's size, each issue's `size:` label on GitHub, as `plan` reads issues (an issue without one counts as `M`, as for its ceilings). It prints, for each issue of the run, its size, its answered calls, its Claude dollars and its Codex tokens, with one line for the calls made for no issue; per role, the attempts and the answered calls, the Claude dollars and the Codex tokens with the median and the maximum per call; per size, the median and the maximum per issue. An attempt that reports no cost adds no dollar and is not among the calls its role's median is taken over; an issue no agent was called for is listed and left out of its size's figures. Nothing is written, and neither `config.py` nor `loop.toml` changes. | `status` shows each issue's calls and dollars and the run's totals, not what `USD_CEILINGS`, `CEILINGS` and `max_budget_usd` are set from: the cost per call of each role and the spread of an issue's cost within a size. Setting them stays the owner's (`docs/factory/green-light.md`, step 10). |

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

## Kept as recorded

`tests/fixtures/epic10.json` is a recording of GitHub's answers for epic #10 of the upstream
repository, made with `implement record-fixture`. It is test input for the graph and the parser
and is not edited: its issues talk about sessions, HTML pages and upstream paths, and it carries
many external links. Several tests also keep upstream's capability names (`hardware:laptop`,
`human:rehearsal`) and example file names as arbitrary test data.

### The `costs` command

No existing test changed; 11 added.

| Test | Change | Why |
| --- | --- | --- |
| `tests/test_costs.py` (11), `tests/fixtures/costs/agents.jsonl`, `tests/fixtures/costs/issues.json` | Added. The run log (twenty attempts) and the five issues with their `size:` labels are written for the test, not taken from a run. | The command on that log, with GitHub replaced by the recorded issues: each issue's size, answered calls and dollars; the totals; each role's median and maximum per call; each size's median and maximum per issue; an attempt that reports no cost (a timeout, a null cost, every Codex call) adds no dollar; an issue nothing ran for stays out of its size; `scripts/implement.sh costs` reaches the command. |
