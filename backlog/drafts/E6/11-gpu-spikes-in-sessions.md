+++
id = "E6-11"
title = "GPU spikes: run in a session the owner booked"
labels = ["size:M", "kind:test"]
epic = "E6"
blocked_by = ["E6-03", "E6-10"]
+++

## Context

The spike jobs that need GPU instances (S3 in its two parts, S5, and the dry-run class) wake their
pool through the gateway today and wait until it is ready. With sessions a request starts no
machine, and the size and the hours of a measurement are the owner's decision: capacity may also
take longer to arrive than a build may wait. So the owner books the session first, and the job
checks it before it measures anything (system design, section 12): the session is active, it has
the instances the run needs in service, and it lasts as long as the run. Otherwise the job stops,
having sent nothing.

The check reads two items of the pool-state table, which the spike jobs' role may already read: the
pool's session (E6-01) and the scaler's note, whose `in_service` E6-03 added. The table's name
reaches the job as `RHI_SPIKE_POOL_STATE_TABLE`, as it reaches spikes S7 and S8 today, and the
longest session comes from the deployed configuration, which these jobs already read.

A job cannot know by itself how many instances its run needs, nor, for S3 and the dry run, how long
it lasts. The owner gives both when starting the build: `RHI_SPIKE_INSTANCES`, 1 unless given, and
`RHI_SPIKE_RUN_MINUTES`, which has no default. The design does not name these variables; this issue
does.

A refusal names one of three fixed reasons, never text from AWS: `no-session`,
`too-few-instances`, `session-too-short`.

After this issue the four jobs run only inside a booked session and send no wake. The gateway still
accepts a wake until E6-18.

## Deliverables

- [ ] `spikes/common.py` has `require_session`, which passes when the pool's session is active, the scaler's note reads at least the instances the run needs in service, and the session ends no earlier than the run's length from now, and otherwise raises with the reason `no-session`, `too-few-instances` or `session-too-short`.
- [ ] `spikes/s3_harness_task.py`, `spikes/s3_second_session.py`, `spikes/s5_capacity.py` and `spikes/dry_run_class.py` each call it before they send any request, with `RHI_SPIKE_INSTANCES`, 1 unless given, and `RHI_SPIKE_RUN_MINUTES`, without which the job refuses to start.
- [ ] A job that is refused sends no request, prints the reason, and ends as a spike that could not measure does.
- [ ] None of the four jobs sends a wake any more: the spikes' Gateway has no wake, and each job waits for the status to read the pool ready where it woke the pool before.

## Acceptance criteria

- [ ] `tests/unit/spikes/test_common.py::test_require_session` shows the check passing, and each of the three reasons from the record or the note that gives it.
- [ ] `uv run --locked pytest tests/unit/spikes` passes: each of the four jobs, refused, has sent nothing to the gateway, and each, admitted, starts without a wake.
- [ ] `git grep -n -e /wake -e "\.wake(" -- spikes` prints nothing.

## References

- `docs/design/system-design.md`, section 12
- `spikes/common.py`
- `spikes/s5_capacity.py`
- `services/common/rhi_common/sessions.py`, delivered by E6-01
