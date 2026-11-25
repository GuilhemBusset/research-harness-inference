+++
id = "E6-10"
title = "Spikes: the jobs of S1, S2 and S4 go, and what the others used of them stays"
labels = ["size:M", "kind:test"]
epic = "E6"
+++

## Context

Three spike jobs have no place in the design any more (system design, section 12):

- **S2** asked whether each client survives the gateway's hold. The gateway will hold no request
  (section 5.3), and the design has no S2.
- **S1** timed starts by waking the pool twenty times, from the warm pool and from cold. There is no
  warm pool and no wake: S1 is now a reading of the stages every launch records, and needs no job
  of its own.
- **S4** timed the weights stage the same way, and is now read as S1 is.

None of the three has run. Their readings over recorded launches are not part of this issue: a
reading is written from a real run's data (section 12), once sessions have recorded launches.

The other jobs use parts of the three modules, which stay, in a module that is not a job:

- `spikes/s3_harness_task.py` and `spikes/s3_second_session.py` take `Client`, `Exchange`,
  `Forwarder`, `refused` and `write_home` from `spikes/s2_client_hold.py`;
- `spikes/s3_harness_task.py`, `spikes/s3_second_session.py`, `spikes/s5_capacity.py`,
  `spikes/s7_drain.py` and `spikes/dry_run_class.py` take `Gateway` and `gateway_address` from
  `spikes/s4_weights.py`.

This issue moves those seven names unchanged, with their tests, then deletes the three jobs.
`Gateway.wake` moves with the rest: the jobs that call it stop doing so in E6-11.

`src/rhi/pipeline/` is a protected path (ADR 0001, decision 10): this issue names the one file it
changes there, the list of spike jobs. `tests/policy/test_pipeline.py` reads the jobs from that
list and does not change.

After this issue the deployed pipeline has three spike jobs fewer. Nothing else changes.

## Deliverables

- [ ] `spikes/forwarder.py` holds Client, Exchange, Forwarder, refused and write_home as `spikes/s2_client_hold.py` had them, and `spikes/s3_harness_task.py` and `spikes/s3_second_session.py` take them from it.
- [ ] `spikes/common.py` holds Gateway and gateway_address as `spikes/s4_weights.py` had them, and `spikes/s5_capacity.py`, `spikes/s7_drain.py`, `spikes/dry_run_class.py` and the two S3 jobs take them from it.
- [ ] `spikes/s1_cold_start.py`, `spikes/s2_client_hold.py` and `spikes/s4_weights.py` are deleted, with `tests/unit/spikes/test_s1.py`, `tests/unit/spikes/test_s2.py` and `tests/unit/spikes/test_s4.py`, and the list of jobs in `src/rhi/pipeline/spikes.py` no longer holds the three.
- [ ] `docs/measurements/s2.md` is deleted, and `docs/measurements/s1.md` and `docs/measurements/s4.md` give the design's pass conditions and say that the spike has no job and is read from the stages every launch records.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes tests/synth/test_spike_jobs.py tests/policy/test_pipeline.py tests/unit/test_measurements.py` passes.
- [ ] `tests/unit/spikes/test_forwarder.py` and `tests/unit/spikes/test_common.py` hold the tests of the seven names that the deleted test files held.
- [ ] `git grep -n -e s1_cold_start -e s2_client_hold -e s4_weights -- spikes src tests docs setup` prints nothing.

## References

- `docs/design/system-design.md`, sections 5.3 and 12
- `spikes/s2_client_hold.py`
- `spikes/s4_weights.py`
- `src/rhi/pipeline/spikes.py`
