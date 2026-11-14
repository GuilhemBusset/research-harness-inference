+++
id = "E3-15"
title = "Spike S1: how long a stopped coursework instance takes to serve again, and a cold one"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E2-37", "E3-40", "E3-45"]
+++

## Context

Cold-start time is the central engineering number of the design: 10 to 13 minutes measured on stock
setups, an estimated 2 to 6 minutes from the warm pool, and 3 to 13 when the pool is empty or its
instance fails to start (section 5.2). No source measured a stopped GPU instance returning to
service, or how often one fails to restart for lack of capacity (evidence E10). Spike S1 asks, on
the coursework pool's instance type: how long each stage takes from stopped to serving, and from
cold; whether the engine image is still on disk after a stop and start; and how often a stopped
instance fails to start (section 12). Pass: a warm start under 5 minutes at the 90th percentile over
20 starts. If it fails: bake the image, lower expectations for homework, rely on pre-warm for
sessions; none of that is drafted.

The stages are the lifecycle agent's, recorded per transition (issue E2-37). The spike starts the
pool with an explicit wake through the gateway and lets it fall idle until the scaler returns the
instance to the warm pool: it never sets a group's capacity. The owner may lower the pool's
idle_minutes in the configuration for the run, which shortens each cycle.

**Warm and cold runs.** The spike measures one kind of start per run, the kind it is given. A warm
run is made on the pool as configured. For a cold run the owner deploys the pool with
warm_instances 0, so that every wake launches an instance straight into service, and puts it back
afterwards; the spike does not change the configuration. It tells the kind of each start from the
transition the lifecycle agent reports for it, and does not count a start of another kind than the
run's.

The measured start is also what the launch hook's timeout becomes once configuration holds it
(sections 4.3 and 17).

Agents build and test the job and the record. Running it needs the account and is the owner's: it
is an obligation here, never a pass.

## Deliverables

- [ ] `spikes/s1_cold_start.py` runs a number of starts of a named pool, 20 by default, of the kind it is given, warm or cold, each started by an explicit wake through the gateway, and reads each start's stage timings and transition from the lifecycle agent's metrics in the workspace, counting only the starts whose transition matches the run's kind.
- [ ] It records, for the run's kind of start, the seconds of each stage and of the whole at the median and the 90th percentile, whether the image stage was zero after a stop and start, and how many starts failed and why, by the last stage reached.
- [ ] It declares the names of the numbers it posts, one per row of its record (each stage and the whole at the median and the 90th percentile; the starts run; the starts failed; the warm starts whose image stage was zero), posts them and the verdict with the code of issue E3-14, and `docs/measurements/s1.md` is the record, its rows labelled with the pool, the instance type, the kind of start and the stage, with a pending verdict until the spike has run.
- [ ] `src/rhi/pipeline/spikes.py` lists the job s1_cold_start, which runs `spikes/s1_cold_start.py`.
- [ ] `tests/unit/spikes/test_s1.py` covers the computation and the verdict on recorded timings.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s1.py tests/unit/test_measurements.py` passes: recorded warm starts with a 90th percentile of 4 minutes pass, of 6 minutes fail, a run of fewer than 20 warm starts gives no verdict, a cold start in a warm run is not counted, and the pending record is valid.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes with the job in the list.
- [ ] The spike is run on the coursework pool, warm and then cold, and `docs/measurements/s1.md` records the measured cold start, warm and cold, which is an exit criterion of phase 2 (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.3, 5.2, 12, 14, 17
- `docs/design/evidence.md`, E10, E11
