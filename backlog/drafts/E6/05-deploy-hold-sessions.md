+++
id = "E6-05"
title = "Hold a deployment while any pool's session is active"
labels = ["size:S", "kind:infra"]
epic = "E6"
blocked_by = ["E6-01"]
+++

## Context

A deployment waits while a session is active on any pool, so that a release never changes anything
under a class or under a measurement (system design, sections 5.6 and 8.2, step 5). The step does
not ask whose session it is: the instructor's, a spike's or the canary's.

Today the step `Hold` runs `scripts/pipeline/deploy_hold.py`, which waits while the session switch
reads on. This issue makes it also read every pool's session item (E6-01). The switch stays read
until E6-21 removes its record: the job waits while the switch reads on or any pool's session is
active.

The step's role does not change: it already reads the items of the pool-state table with
`dynamodb:GetItem`, and a session is one item per pool. So `tests/policy/test_pipeline.py` is not
changed. The job needs the pools' names and the longest session, which the step gives it from the
stage's configuration, as text. `src/rhi/pipeline/` is a protected path (ADR 0001, decision 10):
this issue names the file it changes there.

After this issue the deployed pipeline holds a deployment during a booked session, as it holds one
while the switch is on.

## Deliverables

- [ ] `scripts/pipeline/deploy_hold.py` reads the session item of each pool that `RHI_HOLD_POOLS` names and waits, reading again every 60 seconds, while the switch reads on or any of those sessions is active by the shared function `is_active`; a session item that does not read fails the job, as a switch that does not read does.
- [ ] `src/rhi/pipeline/hold.py` gives the step `RHI_HOLD_POOLS`, the names of the stage's pools separated by commas, and `RHI_HOLD_SESSION_MAX_HOURS`, the configuration's `session_max_hours`; the step's role is unchanged.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_deploy_hold.py` passes: the job waits while one pool's session is active and passes once it has ended, passes at once with no session item and the switch off, and exits 1 on an item that is not a session.
- [ ] `uv run --locked pytest tests/synth/test_pipeline_hold.py tests/policy/test_pipeline.py` passes: the step's environment names every pool of the stage, and its role is the one the policy test already holds.

## References

- `docs/design/system-design.md`, sections 5.6 and 8.2
- `scripts/pipeline/deploy_hold.py`
- `src/rhi/pipeline/hold.py`
- `services/common/rhi_common/sessions.py`, delivered by E6-01
