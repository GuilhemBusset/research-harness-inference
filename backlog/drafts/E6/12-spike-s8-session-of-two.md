+++
id = "E6-12"
title = "Spike S8: a session of two instances on the mock pool"
labels = ["size:M", "kind:test"]
epic = "E6"
blocked_by = ["E6-07", "E6-10"]
+++

## Context

Spike S8 asks whether a replayed stickiness cookie keeps one student on one instance, with two
instances in two zones, and moves when that instance leaves (system design, sections 5.5 and 12).
The job gets its two instances today by holding load on the mock pool until the scaler adds a
second, and its departure by waiting until the load has fitted in one instance for ten minutes.
Neither rule exists under sessions: the pool has what its session asks.

So the job books the mock pool itself, as section 12 says of the jobs that run on it: a session of
two instances, then, for the departure, the same session lowered to one. It books with the module
the verification uses, `scripts/pipeline/booking.py` (E6-07), under the name `spike/s8_stickiness`,
for the minutes the owner gives in `RHI_SPIKE_RUN_MINUTES`, and stops its session when it ends,
whatever came of the run. It refuses to start when the mock pool already has a session, or when the
pool's `max_instances` is below 2. What the job measures, how it tells which instance served and
what makes a run not measured stay as they are.

The scaler releases an instance of a lowered session by the order of section 5.1 only from E6-15
on. Until then a lowered session releases nothing by itself, so the job is to be run once E6-15 is
deployed; its tests use doubles and do not wait for it.

The spike jobs share one role, which may read the pool-state table and write nothing in it. It
gains a write of the mock pool's items there and of no other pool's: the condition is on the
item's partition key (`dynamodb:LeadingKeys`), as on the verification's role. `src/rhi/pipeline/`
and `tests/policy/` are protected paths (ADR 0001, decision 10); this issue names the files it
changes there. The policy test refuses today any write to the pool-state table by a spike job: it
must admit this one and still refuse a write that names another pool or no pool.

After this issue the deployed spike jobs may book the mock pool. No GPU pool can be booked by a
job.

## Deliverables

- [ ] `scripts/pipeline/booking.py` can change the number of instances of a session it booked, with the shared function `changed`, only while the item still reads as its booking.
- [ ] `spikes/s8_stickiness.py` books a session of two instances on the mock pool before it sends anything, for the minutes of `RHI_SPIKE_RUN_MINUTES`, which it requires, and refuses to start when the pool has a session booked or active or allows fewer than two instances.
- [ ] For the departure it lowers its session to one instance and waits until the scaler's note asks for one and reads one in service, in place of the wait on low load, and it stops its session when it ends, whatever the run's result.
- [ ] `src/rhi/pipeline/spikes.py` lets the spike jobs' role write the items of the pool-state table whose partition key is the mock pool's name, and `tests/policy/test_pipeline.py` holds the role to that write and no other.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s8.py tests/unit/test_booking.py` passes: the job books two, lowers to one for the departure and stops its session after a run that passed, failed or was not measured; a session someone else changed is not touched.
- [ ] `tests/policy/test_pipeline.py::test_each_rule_on_the_spike_jobs_refuses_its_breach` refuses a spike role that may write the coursework pool's items, and one whose write names no partition key.
- [ ] `uv run --locked pytest tests/synth/test_spike_jobs.py tests/policy/test_pipeline.py` passes on the app as it stands.
- [ ] In the account, with E6-15 deployed, a build of the job on the mock pool posts the run of S8 with a verdict, and leaves the pool with no active session (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.1, 5.5 and 12
- `spikes/s8_stickiness.py`
- `src/rhi/pipeline/spikes.py`
- `scripts/pipeline/booking.py`, delivered by E6-07
- `tests/policy/test_pipeline.py`
