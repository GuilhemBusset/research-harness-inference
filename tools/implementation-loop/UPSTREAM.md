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
