+++
id = "E3-33"
title = "Spike S8: does a replayed stickiness cookie keep a student on one instance, with two instances in two zones"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E2-75", "E3-05", "E3-29", "E3-30", "E3-31", "E3-32", "E3-45"]
+++

## Context

A student's turns stay on one instance through the internal load balancer's stickiness, with the
gateway replaying each student's cookie (section 5.5; issues E3-29 and E3-30). AWS's page lists a
healthy target in every zone as a requirement for stickiness, which a pool of one or two instances
spread over three zones does not have, and the gateway replaying the cookie follows from documented
behaviour without having been tried (evidence E13; known gap 10). Spike S8 asks, on the mock pool,
with two instances in two zones: does a replayed stickiness cookie keep one student on one instance,
and move when that instance leaves (section 12)? Pass: every request of a student reaches one
instance until it leaves. If it fails: a pool stays at one instance until it does, and a class that
needs more runs on the upgrade pool, whose single instance needs no affinity (section 5.5); neither
is drafted. S8 is part of phase 2's exit (section 14).

The spike moves the pool only through the scaler: load above the pool's target for 60 seconds adds
the second instance, and an idle period takes one away (section 5.1). Which instance served a
request is read from the workspace, where the collector labels every series with its instance
(section 7.2; issue E2-58); the mock's cache imitation also makes the cached share visible per
instance (issue E2-75). The spike's role reads no EC2 API (issue E3-45), so the zone of each instance
comes from the workspace too, from the label zone the node's collector adds to every series (issue
E3-05). Rebalancing needs an instance with waiting requests, which one student sending requests one
after another never causes, so the spike checks that the pool's rebalance number did not change
while it measured.

With two students whose first requests arrive at once, least-outstanding routing places them on
different instances, so whichever instance leaves, one of them has to move.

Agents build and test the job and the record. Running it needs the account and is the owner's or the
pipeline's: it is an obligation here, never a pass.

## Deliverables

- [ ] `spikes/s8_stickiness.py` brings the mock pool to two instances in service by holding enough streams open for long enough, through the gateway, records how many zones the two instances are in by their zone label, and waits for that load to end.
- [ ] It then sends the first requests of two canary students at once, and afterwards each student's requests one after another, a number it is given, one student at a time, reading from the workspace after each student how many requests each instance served.
- [ ] It then sends nothing until the scaler has taken the pool to one instance, sends each student's requests again, and records for each student whether its requests before the departure reached one instance and those after reached the remaining one.
- [ ] It records the pool's rebalance number at the start and the end of each measured phase, and a phase in which it changed is measured again.
- [ ] It declares the names of the numbers it posts (the zones, and per student and phase the requests sent and the most any one instance served), posts them and the verdict with the code of issue E3-14, and `docs/measurements/s8.md` is the record, with a pending verdict until the spike has run.
- [ ] `src/rhi/pipeline/spikes.py` lists the job s8_stickiness, which runs `spikes/s8_stickiness.py`.
- [ ] `tests/unit/spikes/test_s8.py` covers the verdict on recorded counts.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s8.py tests/unit/test_measurements.py` passes: recorded counts in which each student's requests all reached one instance, before and after the departure, pass; counts in which one student's requests were split between two instances before the departure fail; and a run whose two instances shared one zone gives no verdict.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes with the job in the list.
- [ ] The spike is run on the deployed mock pool and `docs/measurements/s8.md` records whether a student stays on one instance with two instances in two zones (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.1, 5.5, 7.2, 12, 14
- `docs/design/evidence.md`, E13
