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
| Engine version here | 1.2.18 |
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

`tests/fixtures/epic10.json` is a recording of GitHub's answers for epic #10 of the upstream
repository, test input for the graph and the parser, and is not edited.
