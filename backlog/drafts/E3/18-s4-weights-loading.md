+++
id = "E3-18"
title = "Spike S4: how long the coursework model's weights take to load from S3, tuned"
labels = ["size:S", "kind:test"]
epic = "E3"
blocked_by = ["E3-40", "E3-44", "E3-45"]
+++

## Context

AWS measured 15 GB in about 5 seconds and 200 GB in about 25 seconds with the streamer tuned;
defaults can be ten times slower, one model was reported slower than the plain loader, and those
timings come from an instance with far more network bandwidth than a single-GPU type has (sections
4.4 and 5.2; evidence E11). The cold-start budget estimates 20 to 100 seconds for the coursework
model's 31 GB (section 5.2). Spike S4: weights load time from S3, tuned (section 12). Pass: under 100
seconds for the coursework model's 31 GB. If it fails, the pool's loader becomes download, then
load, which is a per-pool setting (issues E3-04 and E3-44).

The weights stage is the lifecycle agent's (issue E2-37). The loader setting is in the pool's
configuration, so each setting the spike compares is a configuration the owner deploys for the run,
and the owner gives the spike the name of the setting the pool runs, which labels its rows.

Agents build and test the job and the record. Running it needs the account and is the owner's: it
is an obligation here, never a pass.

## Deliverables

- [ ] `spikes/s4_weights.py` starts a named pool the number of times it is given, by an explicit wake through the gateway, and reads the weights stage of each start from the lifecycle agent's metrics in the workspace, labelled with the name of the loader setting it is given.
- [ ] It declares the names of the numbers it posts (the starts made and the median seconds of the weights stage), posts them per loader setting with the verdict, with the code of issue E3-14, and `docs/measurements/s4.md` is the record, its rows labelled with the pool and the loader setting, with a pending verdict until the spike has run.
- [ ] `src/rhi/pipeline/spikes.py` lists the job s4_weights, which runs `spikes/s4_weights.py`.
- [ ] `tests/unit/spikes/test_s4.py` covers the verdict on recorded timings.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s4.py tests/unit/test_measurements.py` passes: a median of 80 seconds passes and one of 120 seconds fails.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes with the job in the list.
- [ ] The spike is run on the coursework pool, `docs/measurements/s4.md` records the load times, and the pool's loader is set to the faster setting (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.4, 5.2, 12
- `docs/design/evidence.md`, E11
