+++
id = "E3-20"
title = "Spike S7: what scale-in and a gateway deployment do to a request, on the mock pool"
labels = ["size:M", "kind:test"]
epic = "E3"
blocked_by = ["E3-45"]
+++

## Context

Two things are not settled by AWS's documentation (section 5.1; evidence E13): whether the group
waits the whole deregistration delay for a target that has no connections, and whether it drains at
all before stopping an instance into the warm pool. The design does not lean on a long delay: the
scaler removes only instances that report nothing running, the delay is short (`drain_seconds`,
300), the gateway closes idle upstream connections promptly, and the termination hook is the second
guard. Spike S7 measures both paths on the mock pool, for cents, before any GPU depends on them.

Spike S7 asks (section 12): how long does an idle instance take to leave service, when terminated
and when returned to the warm pool? Does a request in flight survive a scale-in and a gateway
deployment (section 8.5)? Is a wake right after a long idle ever lost? Pass: an idle instance stops
within a minute or two, which the verdict reads as 120 seconds; in-flight requests finish; no wake is
lost. If it fails: a shorter delay with protection as the only drain, or balancing in the gateway
without the internal load balancer; neither is drafted.

The spike moves the pool only through the scaler, by load and by idleness. A gateway deployment is
started by the owner, by releasing the pipeline again, while the spike holds a stream open.

Agents build and test the job and the record. Running it needs the account and is the owner's or the
pipeline's: it is an obligation here, never a pass.

## Deliverables

- [ ] `spikes/s7_drain.py` measures, on the mock pool, the seconds an idle instance takes to leave service when it is terminated and when it returns to the warm pool.
- [ ] It holds a slow stream open across a scale-in, and another across a gateway deployment, and records whether each completes.
- [ ] It sends a request at the moment the pool reaches zero after an idle period, as many times as it is given, and counts the wakes that were lost.
- [ ] It declares the names of the numbers it posts (the seconds to leave service on each path, each held stream completed, the wakes sent and the wakes lost), posts them and the verdict with the code of issue E3-14, and `docs/measurements/s7.md` is the record, with a pending verdict until the spike has run.
- [ ] `src/rhi/pipeline/spikes.py` lists the job s7_drain, which runs `spikes/s7_drain.py`.
- [ ] `tests/unit/spikes/test_s7.py` covers the verdict on recorded numbers.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/spikes/test_s7.py tests/unit/test_measurements.py` passes: recorded numbers with an idle instance gone in 90 seconds, every stream complete and no wake lost pass; an idle instance gone in 180 seconds fails; one lost wake fails.
- [ ] `uv run --locked pytest tests/policy/test_pipeline.py tests/synth/test_spike_jobs.py` passes with the job in the list.
- [ ] The spike is run on the deployed mock pool and `docs/measurements/s7.md` records both paths (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.1, 8.5, 12
- `docs/design/evidence.md`, E13
