+++
id = "E0-06"
title = "Report what each issue of a run cost, by role and by size"
labels = ["size:S", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

Each issue has a ceiling of agent calls and of dollars by size (`CEILINGS` and `USD_CEILINGS` in
`tools/implementation-loop/implement_loop/config.py`), and each Claude call a per-role budget
(`max_budget_usd` in `tools/implementation-loop/loop.toml`). The dollar values are provisional: ADR
0001 says the first epic measures the cost from `agents.jsonl` and the ceilings are set from that
measurement, not from an estimate (decision 8, Consequences).

`scripts/implement.sh status` already shows each issue's calls and dollars and the run's totals. It
does not show what the ceilings are set from: the cost per call of each role, and the spread of an
issue's cost within a size. `agents.jsonl` holds one line per attempt with its role, vendor, issue,
cost in dollars (Claude) and tokens (Codex). The run records do not keep an issue's size: it is the
`size:` label of the issue on GitHub, read as the command `plan` reads issues. Codex reports tokens,
not dollars, so its side is reported in tokens.

The run records are outside the working tree, in the git common directory. This issue adds the
command that reads them. Reading the measurement and changing the ceilings are the owner's: both
files are on the safety surface, and `docs/factory/green-light.md`, step 10, has the owner change
them through a pull request of their own.

This issue changes the engine, a protected path (ADR 0001, decision 10); the builder may change the
files named below because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/cli.py` gains a command costs, reached as `scripts/implement.sh costs <issue>`, which prints for each issue of the run its size, answered calls and dollars; per role, the calls, the Claude dollars and the Codex tokens, with the median and the maximum per call; and per size label, the median and the maximum per issue.
- [ ] `tools/implementation-loop/tests/test_costs.py` covers it on a recorded run log and the issues' labels, kept under `tools/implementation-loop/tests/fixtures/` with values built for the test.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_costs.py` checking the totals, the medians and the maxima on the fixture, and that an attempt that reports no cost adds no dollar.
- [ ] The owner runs `scripts/implement.sh costs` on the first epic's run and sets the per-issue ceilings and the per-role budgets from it (needs human:owner-decision).

## References

- `docs/adr/0001-software-factory.md`, decision 8 and Consequences
- `docs/factory/operating.md`
- `docs/factory/green-light.md`
