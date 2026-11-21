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

## No supervision deadline, no fault issue for a stall, no re-run of CI (2026-11-21)

A local patch, not in upstream (ADR 0001, amendment 10). A deadline stops a supervision in the
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
