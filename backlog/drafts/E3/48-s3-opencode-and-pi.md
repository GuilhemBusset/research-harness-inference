+++
id = "E3-48"
title = "Spike S3: OpenCode and Pi run the course's task beside Claude Code, Codex and an SDK script"
labels = ["size:M", "kind:test"]
epic = 5
blocked_by = [187]
+++

## Context

The harnesses the course supports are Claude Code, Codex, an SDK script, OpenCode and Pi (section
6.1; ADR 0003, decision 4). Spike S3 asks whether each completes a multi-turn tool-calling task of
the course's kind on the coursework model (section 12). Issue #187 builds the job,
`spikes/s3_harness_task.py`, for the first three; this issue adds the other two to the same job,
at pinned versions, as `spikes/harnesses.py` pins the others.

Both speak OpenAI Chat Completions to an OpenAI-compatible endpoint (section 6.1). Each takes a
provider entry: the endpoint's `/v1`, the model's name, and the key. OpenCode reads a provider from
its configuration file, where a value can name an environment variable; Pi reads custom models from
its `models.json`, whose key can name an environment variable too. The job writes each
configuration in a folder of its own, gives the key of a spike student through the environment,
and never writes the key into a file.

The npm packages, as each project's own documentation installs them: opencode-ai for OpenCode,
@earendil-works/pi-coding-agent for Pi. Each pin is the newest release published at least 7 days
before the issue is built, the cooling-off of section 17, and the job reports the version each
installed binary prints, as it does for the others.

A spike that needs AWS records what it observed, each measurement with its time, and judges
nothing: the reading is written after the first real run (section 12). So this issue adds the two
harnesses' runs to what the job records, in the form #187 gives the others' records, and no
verdict. The hold behaviour of the two (spike S2) is not measured here; the student guide's
fallback for a client that gives up on a long hold, `rhi wait`, covers them.

## Deliverables

- [ ] `spikes/harnesses.py` pins OpenCode's and Pi's npm packages at exact versions, installs them into the job with the other two, and reports the version each installed binary prints.
- [ ] `spikes/s3_harness_task.py` gives a copy of the task to OpenCode and to Pi, each run non-interactively against the named pool through the endpoint's Chat Completions route, with a configuration the job writes in a folder of its own that reads the spike student's key from the environment, and runs the task's check on what each left.
- [ ] It records, for each of the two, the same observations, each with its time and the harness's version, that it records for the other harnesses.
- [ ] `tests/unit/spikes/test_s3.py` covers the two configurations and the two runs against stubbed processes.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s3.py` passes: each configuration names the endpoint's Chat Completions route and the pool's model, holds no key, and each run records its observations with a time and the harness's version.
- [ ] `tests/unit/spikes/test_s2.py::test_the_harnesses_are_pinned` passes with four exact pins and fails when OpenCode's or Pi's is a range or a tag.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes.
- [ ] The S3 run on the coursework pool includes OpenCode and Pi, and its log holds their observations with times and versions (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 6.1, 12 and 17
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decisions 4 and 8
- `spikes/harnesses.py`
