+++
id = "E3-19"
title = "Spike S5, the runs: replayed agent sessions against a pool, and what each run measured"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E3-06", "E3-27", "E3-32", "E3-34", "E3-36", "E3-37", "E3-40", "E3-45"]
+++

## Context

What one instance serves is an estimate until this project measures it (sections 0 and 4.7). The
coursework table of section 4.7 is arithmetic from the GPU's memory bandwidth and the model's shape,
checked against measurements on other GPUs (evidence E29); its estimate puts the pool at the speed
floor, not clear of it. Spike S5 asks (section 12): what does one coursework instance serve? Every
row of section 4.7's table, with replayed agent sessions of both shapes, hands-on and several agents
a student, at 1, 4, 8 and 16 streams and at the class's size, on the 8-bit and the 4-bit build. What
cache size does the engine report at start? How long are real transcripts and turns? What does each
lever of section 4.7 change?

This issue is the spike's runs and what each run measures. The verdict against the floors, the
record and the posting are issue E3-42, which also carries the obligations to run it.

The levers (the 4-bit build, the effort mapping, a smaller window, the cache precision, the draft
head, the step budget, a shared cache salt, a larger instance) are each a configuration the owner
deploys for a run; the spike labels each run with the lever name it is given, so that the record
compares it with the baseline. The engine's quantities come from the per-engine table (issue E3-06),
the cache capacity from the lifecycle agent's number (issue E2-66), and what each student got from
the load generator (issue E3-36), each session as its own canary student (issue E3-14).

The upgrade pool's spike S6 runs the same measurement, with its own stream counts (1, 8 and 32)
(issue E5-05). So the runs take the pool, the stream counts and the class size as inputs, and
nothing in them is the coursework pool's own.

## Deliverables

- [ ] `spikes/s5_capacity.py` runs the load generator of issue E3-36 against a named pool for each shape, hands-on and several agents a student, at each number of streams it is given, 1, 4, 8 and 16 by default, and at the class size it is given, each for a duration it is given.
- [ ] For each run it reads from the workspace, per instance, the quantities of section 4.7's table that the engine measures, by the names of the per-engine table: output speed per stream, output throughput, prefill speed, time to first token, cached share of input, cache use, preemptions, running and waiting requests, and the distributions of input and output tokens per request.
- [ ] For each run it computes, from the load generator's records, the time to first token at the median and the 95th percentile with first turns apart, the speed per stream at the median and the slowest tenth, and the cost per million output tokens at the configured price of the instance type that ran.
- [ ] It reads the cache capacity in tokens each instance's lifecycle agent reported at start, and labels every run with the pool, the instance type, the build the pool serves (read from the release its lifecycle agents report), the lever name it is given, the shape and the number of streams.
- [ ] `src/rhi/pipeline/spikes.py` lists the job s5_capacity, which runs `spikes/s5_capacity.py`.
- [ ] `tests/unit/spikes/test_s5_runs.py` covers the reading of recorded workspace answers and the computation on recorded load-generator records.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s5_runs.py` passes: recorded workspace answers read back as the quantities of each instance, a first turn of 8 seconds is left out of the 95th percentile of later turns, and the cost per million output tokens at 3.36 dollars an hour matches the recorded throughput.
- [ ] `tests/unit/spikes/test_s5_runs.py::test_the_runs_follow_the_inputs` finds, for the stream counts 1, 8 and 32 and a class size of 20, one run per shape and count, each labelled with the pool, build, lever, shape and count it was given.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes with the job in the list.

## References

- `docs/design/system-design.md`, sections 0, 4.7, 7.2, 12, 17
- `docs/design/evidence.md`, E29, E31
