+++
id = "E3-57"
title = "rhi login sets Claude Code's auto-compact window to 200,000 tokens, so that a session compacts before the coursework pool's context"
labels = ["size:M", "kind:app"]
epic = 5
+++

## Context

The design sizes the coursework pool's context on one premise (section 4.2, "What the harnesses
ask of a model"): Claude Code assumes a 200,000-token window for a model it does not know, so a
served context of 262,144 tokens, above 200,000 and the 32,000 tokens of output Claude Code asks
for, keeps it clear of a context error. Evidence E15 graded that premise T (search summaries) and
left spike S3 to check it.

The premise does not hold. `rhi login` names no model (`client/rhi_cli/harness.py`), so Claude
Code sends its own `claude-` names, which it recognises, and its changelog says that sessions
behind a custom `ANTHROPIC_BASE_URL` use the 1M context window of the models that have one.
Spike S3 sizes its compaction step on the window the pinned Claude Code reports against the
gateway, `CLIENT_WINDOW = 1_000_000` (`spikes/s3_harness_task.py`). Claude Code then compacts only
near a million tokens, at about 967,000 by default. In S3's first run on the coursework pool, the
compaction step's session read its files one at a time, each some 13,000 tokens at most, grew past
the engine's 262,144 tokens, and was refused (issue E3-56 covers the refusal) without ever reaching
its own compaction point. A student's long session would end the same way.

Claude Code's documentation (code.claude.com/docs/en/model-config, "Set the auto-compact window",
and code.claude.com/docs/en/env-vars) gives the setting: `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, a
token count of 100,000 or more, "how full the context gets before compaction". It applies to every
model, recognised names and `[1m]` names included, and takes precedence over `--autocompact`,
`/autocompact` and the `autoCompactWindow` setting. `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is not the
setting: for a name Claude Code recognises it applies only with all compaction disabled. A value
with a suffix (`200k`) needs Claude Code 2.1.304 or later, so the value is written as a plain
number.

The value is 200,000: the window section 4.7 already assumes ("Claude Code compacts a session near
its window, which is 200,000 tokens here"), which with 32,000 tokens of output stays inside the
262,144 served. `rhi login` writes it in Claude Code's user settings, in `env`, beside
`ANTHROPIC_BASE_URL`, as a setting it owns. Spike S3's job writes Claude Code's settings through
`rhi_cli.harness` (`spikes/forwarder.py`, `write_home`), so the job's Claude Code gets the window
too; the compaction step's files must then hold a quarter more than that window, not a quarter
more than a million, or the step reads some 98 files and compacts several times.

Suggested branch: `fix/claude-code-auto-compact-window`

## Deliverables

- [ ] `client/rhi_cli/harness.py` writes `CLAUDE_CODE_AUTO_COMPACT_WINDOW` with the value "200000" in the `env` of Claude Code's user settings, beside `ANTHROPIC_BASE_URL`, as an owned setting whose value is a constant of the module, and its docstring lists it with the reason.
- [ ] `tests/unit/client/test_harness.py` asserts that a login writes the window into new settings and into settings that hold other members, replaces a different value of it, and leaves a file that already holds every owned setting unwritten.
- [ ] `spikes/s3_harness_task.py` takes `CLIENT_WINDOW` from the constant of `client/rhi_cli/harness.py`, so that the compaction step's files hold a quarter more than the window its Claude Code compacts at, and its docstring and comments say so.
- [ ] `docs/student-guide.md` says, in its section on Claude Code, that `rhi login` also sets the auto-compact window, and why.
- [ ] `docs/design/system-design.md` says in section 4.2, in section 6.1's caveat on the served context, in the "A smaller window" lever of section 4.7, and in step 5 of section 6.4 that Claude Code works with the 1M window of its own models behind the gateway and that `rhi login` sets its auto-compact window to 200,000 tokens.
- [ ] `docs/design/evidence.md` replaces E15's row on the 200,000-token window with what Claude Code's documentation and changelog say, at level P with their pages, and its known gap 11 is removed.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/client/test_harness.py tests/unit/client/test_login.py tests/unit/spikes/test_s3.py tests/unit/spikes/test_forwarder.py tests/unit/test_docs.py` passes.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] A rerun of spike S3 on the coursework pool writes a `compaction` line with `compacted` true, `trigger` "auto", `next_turn_completed` true and no refused turn (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.2, 4.7, 6.1 and 6.4
- `docs/design/evidence.md`, E15 and the known gaps
- `client/rhi_cli/harness.py`
- `spikes/forwarder.py`, `write_home`
- `spikes/s3_harness_task.py`, the compaction step
