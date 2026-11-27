+++
id = "E3-61"
title = "Spike S3: with RHI_S3_COMPACT_WINDOW=none, the compaction step's Claude Code runs with no auto-compact window, on a filler sized from the pool's context"
labels = ["size:S", "kind:test"]
epic = 5
+++

## Context

Issue #793 makes the gateway answer an engine's refusal of a prompt longer than the pool's context
with 400 `context_length_exceeded`, on which Claude Code compacts. Its last item, "Claude Code
2.1.323, with no auto-compact window set, run against the coursework pool past the pool's context,
compacts on the gateway's `context_length_exceeded` answer (its event `compact_boundary`) instead
of retrying the turn", has no run that can show it.

Spike S3's compaction step is the run that drives Claude Code past a window, but it always gives
Claude Code a window. The job writes each Claude Code home through `rhi login`'s own module
(`forwarder.write_home`, which calls `rhi_cli.harness.configure_claude_code`), and that writes
`CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` in the `env` of Claude Code's settings (issue #794). The
step's filler is sized from that window (`CLIENT_WINDOW`, `FILLER_FILES`: a quarter more than
200,000 tokens), so Claude Code compacts at 200,000, below the coursework pool's 262,144, and the
engine never refuses a turn. Without the setting, Claude Code compacts only near the 1M window it
gives its own `claude-` names behind a custom base URL, at about 967,000 tokens (issue #794's
context): a session then grows past the pool's context first, the engine refuses the turn, and
Claude Code meets the gateway's `context_length_exceeded`.

So the job gets one variable, `RHI_S3_COMPACT_WINDOW`, in the house style of the spikes' own
variables (`RHI_S5_WINDOW_TOKENS`). Unset, the step runs as it does now. Its one other value, `none`, takes the
auto-compact window out of the compaction step's Claude Code, and only that step's: the task and
the image steps keep what `rhi login` writes, so their lines stay comparable with earlier runs. A
step with no window has no window of its own to size its filler from, so its filler is a quarter
more than the pool's own context, the `context_length` of the pool in the deployed configuration
(`rhi_common.pool_config.Pool`), which the job already reads (`run_job`). At 262,144 tokens that
is 26 files of the present size instead of 20. The `compaction` line says which window the step
ran with, so a reading of the run never has to guess it from the job's variables.

The line already holds what the run must show: `compacted`, `next_turn_completed`,
`window_refusals` (the grown turns refused with a cause in `LENGTH_CAUSES`, which holds
`context_length_exceeded`) and `exit_status`.

Suggested branch: `feat/s3-compaction-without-window`

## Deliverables

- [ ] `spikes/s3_harness_task.py` reads `RHI_S3_COMPACT_WINDOW`: unset, the compaction step's Claude Code has the auto-compact window `rhi login` sets, as now; `none`, the settings file of that step's home holds everything `rhi login` writes but `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, and the command's environment holds no such variable either; any other value stops the job before it sends anything to the gateway, as one line `job_failed`.
- [ ] In `spikes/s3_harness_task.py`, the compaction step's filler and prompt hold a quarter more tokens than the window that step compacts at, by the module's estimate of a line: the auto-compact window when the step has one, and the pool's `context_length` from the deployed configuration when it runs with none; the task and image steps keep the window `rhi login` sets whatever the variable holds.
- [ ] The `compaction` line of `spikes/s3_harness_task.py` holds `auto_compact_window`: the window, in tokens, that the step's Claude Code was given, or `null` when it ran with none.
- [ ] The docstring of `spikes/s3_harness_task.py` gives the variable its row in the table "What the job is given", and its paragraph on compaction says how the step runs without a window, what sizes its filler then, and why the step otherwise never meets the engine's refusal.
- [ ] `tests/unit/spikes/test_s3.py` asserts, for a run with the variable unset and one with `none` on a pool whose `context_length` the test gives: the window in the settings and environment of each step's Claude Code, the filler's estimated tokens above the window or the context, and the line's `auto_compact_window`; and, for another value, one line `job_failed` and no request to the gateway.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s3.py tests/unit/spikes/test_forwarder.py` passes.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] A run of the job `rhi-spike-s3_harness_task` on the coursework pool with `RHI_S3_COMPACT_WINDOW=none` writes a `compaction` line whose `auto_compact_window` is `null`, whose `window_refusals` is 1 or more, and whose `compacted` and `next_turn_completed` are true: Claude Code 2.1.323 compacted on the gateway's `context_length_exceeded` answer instead of retrying the turn (needs gpu:capacity).

## References

- `spikes/s3_harness_task.py`, the compaction step, `compaction_of` and `run_job`
- `spikes/forwarder.py`, `write_home`
- `client/rhi_cli/harness.py`, `AUTO_COMPACT_WINDOW` and `configure_claude_code`
- `services/common/rhi_common/pool_config.py`, `Pool.context_length`
- `docs/design/system-design.md`, sections 4.2, 6.2 and 12 (S3)
