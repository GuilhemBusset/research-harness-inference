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
| Engine version here | 1.2.21 |
| Licence | MIT, Copyright (c) 2026 Guilhem Busset: the same licence and holder as this repository's `LICENSE` |

Taken from that commit: `tools/implementation-loop/` whole, `scripts/implement.sh`,
`scripts/ship.sh`, `scripts/check-loop-guard.sh`, `scripts/test-ship.sh`, and the `implement` and
`ship` skills (`.claude/skills/` and `.agents/skills/`). To see the divergence, export the commit
and compare:

```sh
git -C <upstream clone> archive a0dc6df tools/implementation-loop scripts .claude/skills .agents/skills | tar -x -C <empty dir>
git diff --no-index <empty dir>/tools/implementation-loop tools/implementation-loop
```

The reason for each local change is in the pull request that made it.

## A later verification discharges a smoke-test item, with nothing held (2026-11-22)

A local patch, not in upstream, that extends "A verified deployment discharges a smoke-test item"
below. That discharge reads the verdict the deploying phase recorded on the merge commit, and that
phase holds the merge queue until the pipeline answers, so with `[deploy] contexts` empty, as it
stays, every item that needs `aws:deployed-environment` was left to the owner. Here the same proof
is read after the fact, on each run, and nothing waits for it. The engine's version stays 1.2.21:
nothing here repairs a failed issue.

| File | What changed |
| --- | --- |
| `implement_loop/config.py` | `Deploy` gains `discharge_contexts`, empty by default. `load_deploy` reads it as a list of contexts of the form `verify/<stage>`, the stage named as CodePipeline names one, as the reporter checks it (`DISCHARGE_CONTEXT`): another type is refused (`TypeError`), and so is another form (`ValueError`). The key and the code that reads it land together, since `load_deploy` refuses a key it does not know. |
| `implement_loop/engine.py` | `run` calls the new `_discharge_delivered` before `_reconcile_delivered`. For each delivered issue of the plan, not edited since its delivery, that owes an item `_smoke_items` accepts (the item conditions of `_verified_discharged`, moved into that helper), it reads each discharge context on the merge commit of the issue's pull request, found by its number (`get_pr`), since a later pull request may reuse its branch; and each context that is not a success there with exactly `verified_description` on the fetched head of `main`, if that commit's history holds the merge commit. Each item gets the first of those successes whose commit holds its smoke tests as regular files (`_holds_tests`, for both discharges: not a directory of that name nor a symbolic link, and read with `ls-tree -z`, so a path is compared as it is and not as git quotes it). Then a `commit_status` evidence record joins the verifier's records of the item, which stay; its row joins the pull request's evidence comment, which keeps the rows another root's run discharged for that pull request (`_status_elsewhere`): its box is ticked with a link to that comment, once the issue is read again and still says what was delivered; and its obligation is removed. Each run saves its records before it writes the comment and the body, and reads the other roots' records again once it has written both, writing both again with any saved meanwhile (`_publish_status_rows`, at most `DISCHARGE_WRITES` times), so two roots discharging at once, whichever finishes last, erase no discharged item's row or tick. A proof once read is kept, with the pull request and the smoke tests it proves (`_kept_proofs`): a write GitHub did not answer, or applied without its reply (a ticked box), is recorded by the next run from that proof, whatever the statuses then say, and until then `_reconcile_delivered` does not close the issue on its ticked boxes. The event `obligations_discharged` journals it, and `_reconcile_delivered` closes the issue once every box is ticked, and records it `done` only once GitHub says it is closed: a close that raises is read again, and an issue that is not then known to be closed waits (`waiting`, to resume `delivered`), so that the next run closes it if it is open, or, if it is closed after all and so no longer that run's work, settles it `done` (`_settle_closed_elsewhere`). Reads and writes are made directly, not through `_safe`, so a call GitHub does not answer (a comment list included, which `_upsert_comment` would take for an empty one) discharges nothing and is journaled (`discharge_unread`); the next run tries again. It never enters the deploying phase, takes no claim and does not poll. The fields are read with `getattr` and a default, so this `engine.py` beside an older `config.py` discharges nothing; the fallback `Deploy` gains the field. The client makes no new kind of write: an issue's body and state, and a pull request's comment, as the evidence step does. |
| `implement_loop/github.py` | A read, `get_pr`: a pull request by its number, in the client and the fake. No write is added. |
| `loop.toml` | `[deploy]` sets `discharge_contexts = ["verify/Rhi"]`, `verified_description` to the reporter's `VERIFIED_DESCRIPTION`, and `unverified_tests` to the GPU-only smoke test's file, which the verification's run on the mock pool leaves a test out of. `contexts` stays empty and `approval_description` keeps its value; the comments describe the new key. |
| `tests/test_discharge.py` | New: no merge waits on a discharge context and no status is read before delivery; a later verification on the merge commit, or on a `main` that holds it, ends the issue `done`; what discharges nothing (another description, pending, failed, a test left out, one the commit does not hold or holds as a directory, another capability, another context, a symbolic link to no file, a `main` without the merge commit, an unanswered read); an unanswered read or write is tried again with a single evidence comment; and an issue edited while the statuses are read, the verifier's record of an item's agent part, a branch reused by a later pull request, a non-ASCII test path, a context read on `main` when another succeeded on the merge commit, an unanswered close and its progress comment, a close whose reply was lost, read again or not, a tick whose reply was lost, with `main` moved on before the next run or not, rows another root's run published, and two roots' runs discharging at once, holding either the comment or the body. |
| `tests/test_deploying.py` | The two tests that held the committed table to its neutral values hold it to the new ones, and read and refuse `discharge_contexts`, a stage name CodePipeline does not accept included. |

Behaviour an upstream reader would not expect: a delivered issue can end `done`, with no action of
the owner, on a later run, after the pipeline verified a commit that holds its merge.
`tests/unit/test_deploy_settings.py`, a product test, holds each discharge context to a
`verify/<stage>` context the reporter posts.

## What the removed controls left on the safety surface goes (2026-11-21)

A local patch, not in upstream, that finishes the two before it ("Nothing locks a test..." and
"Nothing runs again on `main`..."), by the same decision of the owner. Those left in place what
was on the safety surface and no longer used: the zone half of the gate, the `resolve_locked`
role, and the plan's `zones` and the build's and decisions' `dispositions` in the answers. They
go. What the gate decides does not change: on every set of changed files and grants the new
check returns what the old one returned when the engine called it, word for word. The engine's
version stays 1.2.21: nothing here repairs a failed issue.

| File | What changed |
| --- | --- |
| `implement_loop/gate.py` | `zone_problems` becomes `protected_problems(changed, grants, repair)`: a protected path changed without a protected grant on it or a directory above it, and in a fault-repair run anything on the safety surface. `path_allowed` and the "outside the plan's zones" problem go, and so do `lint_plan`'s `zones` argument and its check. |
| `implement_loop/engine.py` | Calls the two as they now are. |
| `implement_loop/live.py`, `loop.toml`, `prompts/resolve_locked.md` | The `resolve_locked` role goes: its settings, its method, its table and its prompt. A `loop.toml` that still has the table loads, as any unknown role does. |
| `implement_loop/schemas.py`, `implement_loop/live.py`, `implement_loop/agents.py` | The plan's `zones`, the build's `dispositions`, a decision's `dispositions` and the resolver's schema go from the answers and from `PlanResult`, `BuildResult` and `Decision`. |
| `prompts/plan.md`, `build.md`, `decide.md` | No longer ask for the fields that went. |
| `tests/test_protected_paths.py` | New `ProtectedProblemsTests`: an ordinary file is never a problem, a protected path without a grant fires (with the same words, eight paths at most), a grant covers its path and its subtree and no sibling, a grant not marked protected approves nothing, and a repair run never changes the safety surface, granted or not. The other tests lose the fields and the role. |

## Escalated Codex calls: a protected path is decided on the strongest model (2026-11-21)

A local patch, not in upstream. No person reviews a change to a protected path, so the calls that
decide about one run on Codex `gpt-6.2-astra`, and on `gpt-6.2-sol`, the default model, while
astra refuses calls (ADR 0001, amendment 13). Every other Codex call stays on the default model;
`plan` and `build` stay on Fable, then Opus. The engine's version stays 1.2.21: nothing here
repairs a failed issue.

| File | What changed |
| --- | --- |
| `implement_loop/live.py` | `RoleSettings` gains `codex_escalated_model` and `codex_fallback_model`. `call` takes `escalate`: an escalated Codex call asks for the role's `codex_escalated_model`. A Codex call that gave no answer is refused when an `error` or `turn.failed` event of its stream, or its stderr, says so (`codex_refused`: a usage limit or quota, a rate limit, a model the account may not use); like a refused Claude call it is an outage charged to nothing, and with a `codex_fallback_model` the same attempt is made again at once on it, kept for `REFUSED_RETRY_S`. Refusals are remembered per vendor and model (`_model`, which replaces `_claude_model`). `examine` escalates when `git diff --name-only --no-renames <base>..<head>` lists a path `is_protected` or `is_safety_surface` holds, or cannot be listed; `decide` when a `protected_change` is among its requests; `arbitrate` always. `agents.jsonl` keeps the error messages of a Codex attempt (`errors`) and marks an escalated one (`escalated`). |
| `loop.toml` | `examine`, `decide` and `arbitrate` get `codex_escalated_model = "gpt-6.2-astra"` and `codex_fallback_model = "gpt-6.2-sol"`; the header documents both keys. |
| `tests/test_codex_escalation.py` | New, against a fake `codex` that refuses a model as `codex exec --json` reports it: which calls escalate (an arbitration; a decision with a `protected_change`; an examination of a change that adds, changes, moves out or removes a protected file, or whose paths cannot be listed), and which do not; the fallback at once, charged to nothing, kept 30 minutes for every role; both models refusing, a refused default model and a crash; what counts as a refusal; the error messages kept from the stream. |
| `tests/test_agent_git_isolation.py` | Its stand-in for `call` accepts `escalate`. |

## An examiner bound: from the fourth examination, only a safety invariant blocks (2026-11-21)

A local patch, not in upstream. Several issues failed on their call ceiling while every round
closed the findings it was given and each examination raised new edge cases. From an issue's
fourth examination since its latest plan, a finding that names only an item becomes a follow-up
issue, and a head whose items are all met is certified even when the examiner withholds its
certificate (ADR 0001, amendment 12). The engine's version stays 1.2.21: nothing here repairs a
failed issue. An issue in flight keeps the examinations it already had, read from the journal.

| File | What changed |
| --- | --- |
| `implement_loop/config.py` | `Caps` gains `examiner_bound`, 4. |
| `implement_loop/model.py` | `IssueState` gains `examinations`, the examinations since the latest plan; `None` in a state an earlier engine wrote. |
| `implement_loop/engine.py` | The plan step sets the count to 0. The examine step counts each examination (`_examinations`, which reads a missing count from the journal: the `examined` events after the issue's last `planned` one) and tells the examiner which one it is and what blocks in it (`_examination_note`, the first line of the examination's feedback). From the bound on, a finding the gate would block and that names no safety invariant becomes `follow_up`, its reason says why, and the `examined` event lists it under `bounded`; a withheld certificate with no unmet item, no missing verdict and no blocking finding certifies the head, with a decision (`examiner-bound-<n>`, quoting the examiner's summary) and the event `certified_by_bound`. The follow-up issue names the item of each finding and says that past the bound only a safety invariant blocks. |
| `prompts/examine.md` | States the bound, and renders `{{feedback}}`: what the engine tells the examiner. Until now the examination prompt had no such placeholder, so the request to complete an incomplete examination and the note that an examination certifies a revert never reached the examiner. |
| `implement_loop/agents.py` | The fake examiner can raise another reproduced finding on an item at each examination (`new_findings`), or on a safety invariant (`invariant_findings`); the fake builder handles each one it is shown. |
| `tests/test_examiner_bound.py` | New: the fourth examination's finding on an item is a follow-up and the issue lands; a finding on a safety invariant still blocks past the bound; a withheld certificate no longer holds a met head there, and an unmet item still does; a replan starts the count again; a state with no count reads it from the journal; the examiner is told which examination it is. |

## No supervision deadline, no fault issue for a stall, no re-run of CI (2026-11-21)

A local patch, not in upstream (ADR 0001, amendment 11). A deadline stops a supervision in the
middle of an epic, while its runs bound and the per-issue ceilings already keep it finite; what
sequences supervisions runs outside the engine and may still give one. A stall (`ceiling`,
`no_progress` and the like) is no engine defect and never got a repair run, so its fault issue
only waited for the operator to close it. The re-run of a failed `required` check needed write
access to Actions, which the factory's GitHub App does not have, and a red check goes to the
builder with its log anyway. The engine's version stays 1.2.21: nothing here repairs a failed
issue.

| File | What changed |
| --- | --- |
| `implement_loop/supervisor.py` | `MAX_HOURS` is gone. `max_hours` is optional (default `None`): with none there is no deadline, `_run_engine` waits for the run however long it takes, and no backoff is cut short. A deadline given by the caller works as before (one for repair and root runs, the run asked to stop, then signalled). The 10-run bound is unchanged. `_stall` stays, for the stall entries `faults.json` still holds. |
| `implement_loop/cli.py` | `supervise --max-hours` defaults to none, and `--detach` passes it on only when given. |
| `implement_loop/engine.py` | `_fail` records no fault (the `stall:` fingerprint and its issue body are gone); the event `failed` keeps the cause and the detail. The label `loop:failed` no longer says "filed as a loop fault". The land step no longer re-runs a failed required check (`ci_rerun_heads`, the event `ci_rerun`): its conclusion goes to `_ci_failed` at once. `_wait_required` returns the conclusion only, and its `after` parameter is gone. The builder's feedback no longer says "also after one re-run". |
| `implement_loop/github.py` | `rerun_failed` is gone from the client's protocol, `GhCli` and `FakeGitHub` (with its `reruns`). |
| `implement_loop/model.py` | `IssueState.ci_rerun_heads` is gone; a state file that still carries it loads (`from_dict` drops unknown keys). |
| `implement_loop/report.py` | The progress comment of a failed issue, the run summary and the plan's preview no longer say that a failed issue is filed as a loop fault. |
| `tests/test_supervisor.py`, `tests/test_review_fixes.py`, `tests/test_sandbox.py` | No deadline without `max_hours`; the CLI passes `--max-hours` on only when given; a stall fault from an earlier engine still gets no repair run; `_fail` files no fault and journals `failed`. |
| `tests/test_engine.py`, `tests/test_engine_recovery.py`, `tests/test_github.py`, `tests/test_scheduler_state_verify.py` | The re-run tests are gone; a red check goes straight to the builder; a cancelled check waits until it is run again; an old state with `ci_rerun_heads` loads; a failed issue files no fault. |
| `tests/test_history_checks.py`, `tests/test_redaction.py` | A failed issue's place and rule are read from its progress comment, which is what reaches GitHub, and no `loop:engine-bug` issue is filed. |

## Nothing locks a test, and no zone restricts the builder (2026-11-21)

A local patch, not in upstream, by the owner's decision of 2026-11-21 (ADR 0001, amendment 10).
The tests the builder wrote locked at the first examination: an edit was reverted unless the
other vendor approved a `test_change` request, a conflict in one went to the other vendor
(`resolve_locked`), and every test a command once ran had to keep running. The plan's zones
bounded what a builder could change, behind a `paths` request, and a changed command needed a
`command_change` request. A third of the failed rounds came from the locks alone, and almost
every `paths` request was approved. The builder may now change any file, its tests and commands
included; the examiner reviews every change to a test, and protected paths stay as they were.
The engine's version stays 1.2.21: nothing here repairs a failed issue.

| File | What changed |
| --- | --- |
| `implement_loop/engine.py` | Gone: the lock commit at the first examination (`_lock_tests`) and the late one, the revert of edits to locked tests, `test_change`, the relock after main's merge, the other vendor's resolution of a conflict in a locked test, the selection manifest and the "unselected" check, the plan's zones (`_declare`, conflict and `paths` grants, the zone failure), and `command_change`. The examined head is the head that lands, with no delta examination after a lock commit. A protected path is granted at planning when the issue's deliverables name it (it used to need the plan's zones too); the verify step passes every changed file as in the lane to `gate.zone_problems`, so only a protected path without a grant, or the safety surface in a repair run, fails it. A stored request of a retired kind is refused with the reason (`RETIRED_REQUESTS`). `_release_locks`: before a build on a branch whose acceptance-checks commits still lock files in CI's loop guard (`locked_on_branch`, read as the guard reads them), one commit that changes no file releases them (`locks_released`). Unreviewed files of an external merge are those of the issue's own change, not of its zones. |
| `implement_loop/workspace.py` | `commit_unlocks`: that release commit, on top of HEAD, leaving the index and the working tree as they are. |
| `implement_loop/verify.py` | `verify` takes no locks, check files or manifest and returns `(ok, evidence, problems)`. Gone: `digest`, `lock`, `tamper_report`, `config_candidates`, `selection`, `unselected`, `not_run`, `defined_tests`, `base_id`. |
| `implement_loop/config.py` | `TEST_CONFIG_NAMES` goes. |
| `implement_loop/model.py` | `IssueState` loses `tests`, `locked`, `manifest`, `dispositions`, `reverted` and `last_ran`. A `state.json` that holds them, or a plan with `zones`, still loads: unknown keys are dropped. |
| `implement_loop/schemas.py`, `implement_loop/agents.py` | The request kinds `paths`, `command_change` and `test_change`, and a request's command fields, go. The fake agents lose `resolve_locked` and the lock scenarios; a blocked build asks with `protected_change`. |
| `prompts/build.md`, `plan.md`, `decide.md`, `examine.md` | No locks, no zones, no command or test requests. The examiner is told that a weakened test is a finding: under the safety invariant about a weakened check when the test proves a gate or an invariant, otherwise quoting the item. |
| tests | The tests of locks, zones and command requests go (`test_relock_digest.py`, `test_main_suite_selection.py`, `test_test_change_proposals.py`, `test_command_tests_oserror.py` whole); `test_protected_zones.py` becomes `test_protected_paths.py` and `test_deleted_on_main.py` becomes `test_committed_probes.py`. New: an earlier engine's lock is released before the builder edits the test, and CI's guard passes (`test_engine_recovery.py`); the engine reads the guard's locks as the guard does (`test_live.py`); old state still loads (`test_scheduler_state_verify.py`). |

Left in place on purpose, because they are on the safety surface and this change does not need
them changed: `live.py` still has the `resolve_locked` role and parses the plan's `zones` and the
build's `dispositions` (so `schemas.py` keeps those fields, `prompts/resolve_locked.md` stays,
and the prompts tell the agents to leave them empty), `loop.toml` keeps `[roles.resolve_locked]`,
`gate.py` keeps its zone check (`path_allowed`, the "outside the plan's zones" message) and
`lint_plan`'s zones argument, and `scripts/check-loop-guard.sh` keeps enforcing the locks of
earlier acceptance-checks commits. The engine calls none of the first ones.

## Nothing runs again on `main` after a merge, and a repeated command runs once (2026-11-21)

A local patch, not in upstream, by the owner's decision of 2026-11-21 to remove the engine's
controls over the builder that only produced false failures. After each merge the engine ran the
issue's checks again on `main`, then the locked test ids of every earlier issue of the run, and
blamed the last merge for any failure: a fix-forward round, then a trial revert. None of those
failures was a defect (engine tests under load, deleted probes, renamed test ids), they held the
only writer for up to 77 minutes per merge, and CI already runs every test on each push to `main`.
And a command that several criteria named ran once for each of them in every verification. The
engine's version stays 1.2.21: nothing here repairs a failed issue.

| File | What changed |
| --- | --- |
| `implement_loop/engine.py` | The evidence step (`_accept`) runs no check. Its evidence is the verifier's records of the head that landed (`IssueState.evidence`, kind `command`), and the evidence comment names that head. Gone: the re-verification on `main` (event `post_merge_verify`), the re-run of earlier issues' checks (`_regressions`, events `regression_check` and `regression_check_skipped`, the run state's `accepted_checks` section) and the helpers that chose which checks to skip (`named_tests`, `named_nodes`, `defines_test`). A failed deployment still gets one fix-forward and then a revert (ADR 0001, decision 5), and so does a ledger item without evidence; the revert no longer re-runs earlier issues' checks before it is certified. The evidence step still refreshes the coordination worktree. An `accepted_checks` section in an older `state.json` is ignored. |
| `implement_loop/verify.py` | `verify` runs a command once per verification when several checks give it (the same command, directory and timeout). Each criterion still gets its own evidence record of that run, and its own problem when it failed. |
| `implement_loop/agents.py` | The fake builder writes its edge-case file for feedback that names an edge, not for a word that holds one ("ledger"). |
| `prompts/build.md` | A command listed for several items runs once. |
| `tests/test_verify_accounting.py` | New: one run is the evidence of every criterion that names the command, a failure is a problem of each, and another directory or timeout is another run. |
| `tests/test_engine.py` | `EngineCase.failing_deployments`: the fake pipeline fails the deployment of an issue's merges. The tests of the revert's gates (`test_review_fixes.py`, `test_standing_checks.py`, `test_history_checks.py`) reach the revert through it instead of through a broken earlier issue. |
| `tests/test_deleted_on_main.py`, `tests/test_engine_recovery.py`, `tests/test_owner_landing.py` | The tests of the re-checks on `main` are removed; the edit of an issue during the evidence step is made while its evidence comment is written. |

## A root takes over the deliveries of another root's run (2026-11-19)

A local patch, not in upstream. A delivered issue stays open on GitHub while it still has owner
items, so a later epic whose work it is planned and built it again from its own, empty record. It
usually found its branch already merged. #122 did not: it was built again on `loop/issue-122`,
whose history still held the first run's lock commit, so CI's loop guard refused every head and
the issue failed. That blocked #126 and #130. The engine's version stays 1.2.21: nothing here
repairs a failed issue.

| File | What changed |
| --- | --- |
| `implement_loop/engine.py` | At the start of a run, `_adopt_deliveries` gives each work item not yet merged here the record of another root's run that delivered or completed it (that run's latest round, and this run's own worktree), journaled as `delivery_adopted`, and publishes its phase. It does this once per issue, kept in the run's `adopted` section, so a round this run starts later, after an edit, is its own. |
| `tests/test_adopted_delivery.py` | New: such an issue, failed here, is taken over as delivered once, and nothing is planned or built again; without a delivery elsewhere, it is built here. |

## A failed CI run's feedback is the failed job's log (2026-11-19)

A local patch, not in upstream. When the required check fails, the builder gets the tail of the
failed jobs' logs. The required check is a failed job too: its lines come last in the log and only
name the jobs that failed, so they filled the tail and the failing job's own lines were lost.
#102's builder could not see that an apt mirror's 503 had failed `engine-tests`, and it failed on
`no_progress`. The engine's version stays 1.2.21: nothing here repairs a failed issue.

| File | What changed |
| --- | --- |
| `implement_loop/github.py` | `failed_log` takes `skip`, a job whose lines are left out before the tail is cut. |
| `implement_loop/engine.py` | `_ci_failed` passes the required check's name as `skip`. |
| `tests/test_github.py` | New: the tail keeps the failed job's last line and none of the required check's. |

## An issue closed by another run no longer keeps a root running (2026-11-19)

A local patch, not in upstream. A root's state keeps every issue it ever recorded, prerequisites
pulled in from another epic included. When another run closed one of them, its stale phase
(`waiting`, `pending`, `build`) stayed in this root's state: the supervisor never found the root
settled and ran it to its bound, and the watcher relaunched it, so the next epic never started.
The engine's version stays 1.2.21: nothing here repairs a failed issue.

| File | What changed |
| --- | --- |
| `implement_loop/engine.py` | At the start of a run, `_settle_closed_elsewhere` marks `done` (no wait left) every recorded issue that is not this run's work, has not ended (`done`, `delivered`, `failed`), and is closed on GitHub; the event `closed_elsewhere` journals it. An issue still open is left as it was. |
| `tests/test_closed_elsewhere.py` | New: such an issue ends `done` and the root settles; one still open is left alone. |

## A verified deployment discharges a smoke-test item (2026-11-18)

A local patch, not in upstream. ADR 0001 (decision 6, Consequences) asks to move AWS-only items
from the owner to the pipeline's commit statuses as smoke tests appear. The engine's version stays
1.2.21: nothing here repairs a failed issue.

| File | What changed |
| --- | --- |
| `loop.toml` | `[deploy]` gains `verified_description`, an empty string: the description of a `verify/<stage>` success whose smoke tests ran and passed. The owner sets it with the contexts; the loop never fills it. The table also describes `unverified_tests`, which it does not set: the smoke test files of which the verification leaves a test out. |
| `implement_loop/config.py` | `Deploy` gains the two fields, and `load_deploy` reads them: it refuses a description that is not a string, and a list that is not one of strings. An absent `unverified_tests` is `None`, which is not the empty list. |
| `implement_loop/engine.py` | The deploying phase records, with its verdict, the description each success carried (`_deployed`). The evidence step calls the new `_verified_discharged`: an owner item whose capability is `aws:deployed-environment`, whose text ends with no other capability and names a test file under `tests/smoke/` (`SMOKE_TEST`) that the merge commit holds, is proven when a configured context of the form `verify/<stage>` succeeded on that commit with a description equal to a non-empty `verified_description`, and when none of the tests it names is among `unverified_tests`: the verification does not run every smoke test, and a table that does not say which it leaves out (`None`) discharges nothing. It gets an evidence record (kind `commit_status`: the commit, the context, the description), a row in the evidence comment and a ticked box; its obligation is removed, as for an item a CI check run discharged, and the event `obligations_discharged` journals it. Anything less discharges nothing, and no status is read again in that step. |
| `prompts/plan.md` | The `owner` bullet says that such an item is proven by the deployment verdict, and that an item about the deployed system should name the smoke test that decides it. |
| `tests/test_deploying.py` | Tests added, none removed or changed: the keys and their reading; an issue whose only obligation is such an item ends `done`; and the cases that stay the owner's (another description, none configured, no verification context, no smoke test named, one the commit does not hold, one the verification leaves out, a table that does not say what is left out, another capability). The helper `Loop` gains optional arguments. |

Behaviour an upstream reader would not expect: once the owner has set the contexts and the
description, an issue with an item that needs `aws:deployed-environment` can end `done` with no
action of the owner. The engine does not import the reporter: `tests/unit/test_deploy_settings.py`,
a product test, holds the descriptions of `loop.toml` equal to the reporter's constants, and
`unverified_tests` equal to the files of the tests the verification's own run deselects
(`scripts/pipeline/verify.py`).

`tests/fixtures/epic10.json` is a recording of GitHub's answers for epic #10 of the upstream
repository, test input for the graph and the parser, and is not edited.
