+++
id = "E3-41"
title = "Spike S3: does each harness complete a task of the course's kind on the coursework model"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E3-16", "E3-17", "E3-21", "E3-27", "E3-34", "E3-36", "E3-40"]
+++

## Context

A route existing is not a harness working (section 6.1). The vLLM project replayed real Claude Code
traffic and found a defect for every model family; for the coursework model the request profile
handles the three that stop a session (sections 4.2 and 6.2, step 4; evidence E28). Claude Code
against non-Claude models is unsupported by Anthropic (evidence E15), and the model's quality rests
on its publisher's numbers, so this spike is worth more here than any leaderboard (section 4.2).

Spike S3 asks (section 12): does each harness complete a multi-turn tool-calling task of the course's
kind on the coursework model? The task is issue E3-17's folder. With Claude Code's default settings:
is every request accepted, including its short side queries; does an image inside a tool result
arrive; does a session that fills its window compact and continue; and how much of a replayed
session's input is served from the cache? Each on the 8-bit and on the 4-bit build. The run shaped
like the course's second session is issue E3-35, which adds to the same record.

Pass, for this part: Claude Code, Codex and an SDK script pass, or the owner waives one in writing;
at least 85% of the replayed session's input is cached. Claude Code is the anchor harness, so S3
passes only if Claude Code completes the task, or the owner waives it in writing (section 6.1). If it
fails: an addition to the request profile, another engine version, the fallback model of section
4.2, or a harness marked unsupported for that pool; none of that is drafted.

The harnesses are the versions spike S2 pins and installs (`spikes/harnesses.py`, issue E3-16), and
the record keeps each version. The 4-bit build runs as the pool's model once the owner has set it in
the configuration (its manifest is issue E3-34); the spike reads which build a pool serves from the
release its lifecycle agents report.

Agents build and test the job and the record. Running it needs the account and is the owner's: it
is an obligation here, never a pass.

## Deliverables

- [ ] `spikes/s3_harness_task.py` installs the harnesses spike S2 pins, gives a copy of the task of issue E3-17 to Claude Code with its default settings, to Codex with the profile `rhi login` writes and to an SDK script, against a named pool, and decides each result by running the task's check on what the harness left.
- [ ] For the Claude Code run it records how many of its requests the gateway or the engine refused, side queries included, whether an image returned inside a tool result was read back by the model, and whether a session driven past its window compacted and completed its next turn.
- [ ] It replays a recorded Claude Code session through the pool with the load generator of issue E3-36 and records the share of its input the engine served from its cache, by the cached and input token counters of the per-engine table.
- [ ] It declares the names of the numbers it posts (per harness: task passed and turns taken; for Claude Code: requests refused, image read, compaction completed; the cached share), posts them and the verdict with the code of issue E3-14, and `docs/measurements/s3.md` is the record, its rows labelled with the harness, its version and the build the pool serves, with a pending verdict until the spike has run and a place for the owner's written waiver.
- [ ] `src/rhi/pipeline/spikes.py` lists the job s3_harness_task, which runs `spikes/s3_harness_task.py`.
- [ ] `tests/unit/spikes/test_s3.py` covers the verdict on recorded runs.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s3.py tests/unit/test_measurements.py` passes, and the pending record is valid.
- [ ] `tests/unit/spikes/test_s3.py::test_verdict` fails a recorded run with one failing harness unless a waiver is recorded for it, fails a cached share of 84% and passes one of 86%, and fails a Claude Code run with one refused request.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes with the job in the list.
- [ ] The spike is run on the coursework pool with the 8-bit and the 4-bit build, and `docs/measurements/s3.md` records each harness's result, which is an exit criterion of phase 2 (needs gpu:capacity).
- [ ] The owner says which harnesses must work, decision 4 of the design, and signs any waiver in `docs/measurements/s3.md` (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 4.2, 6.1, 6.2, 12, 14, 15
- `docs/design/evidence.md`, E15, E28
