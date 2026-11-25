+++
id = "E6-08"
title = "Verification: book the coursework pool, and tell no capacity from a failed release"
labels = ["size:M", "kind:infra"]
epic = "E6"
blocked_by = ["E6-07"]
+++

## Context

When a deployment changes a GPU pool's release, the verification runs the smoke tests once more on
the coursework pool. Today the first stream wakes the pool and the step waits behind the gateway's
hold. Three verifications were lost that way: the instance arrived one to seven minutes after the
check stopped waiting (`docs/design/evidence.md`, E32). A GPU that AWS does not have yet says
nothing about the release.

So the step books the coursework pool as it books the mock pool (E6-07), waits `check_wait_minutes`
for the instance, and then tells three cases apart (system design, section 8.2, step 6):

- the pool is ready: the tests run, and their result stands;
- no instance was launched during the wait: the GPU tests are not run, for lack of capacity. The
  step stops its session, the execution passes on the mock pool's tests, and the status on the
  commit says so with a description of its own;
- an instance was launched and the pool is not ready: the tests run and fail, and the last
  lifecycle stage each instance reached is reported, as today.

"An instance was launched" is read from the heartbeats table, which the step may already query: the
lifecycle agent writes its instance's row from the first stage of a launch, so a pool with no row
written since the booking had no launch.

The decision whether an execution verifies on the coursework pool
(`scripts/pipeline/gpu_verification.py`) is not changed here: E6-24 changes it with the instance
refresh.

The reporter gives a verification's success one description from a fixed list
(`rhi_reporter.status`). This issue adds one member, for a verification whose mock tests passed and
whose GPU tests did not run for lack of capacity, and a mark the step leaves for it beside the
existing ones, `verification/<execution ID>/gpu-not-run`. The implement loop discharges an item
that names a smoke test only on a success that carries the description of a verification whose
tests ran and passed: a success with the new description discharges nothing, which is right, and
`tools/implementation-loop/loop.toml` is not changed.

`src/rhi/pipeline/` and `tests/policy/` are protected paths (ADR 0001, decision 10). This issue
names the files it changes there: the role's condition on the partition key now admits the
coursework pool's name beside the mock pool's.

After this issue the pipeline books a GPU instance by itself, for the minutes of its tests, after a
deployment that changes a GPU pool's release. That is decision 14 of section 15, which is the
owner's.

## Deliverables

- [ ] `scripts/pipeline/verify.py` books each pool of the decision's file as it books the mock pool, with the same booking module, and stops the session it booked after that pool's tests or its wait.
- [ ] When the wait ends with the pool not ready and no heartbeat row of the pool written since the booking, the script runs no test on that pool, leaves the one line `no-capacity` in the file `RHI_VERIFY_GPU_REASON_FILE` names, and exits with the result of its other runs.
- [ ] `src/rhi/pipeline/verify.py` copies that file, when there is one, to the mark gpu-not-run of the execution under the artifact bucket's verification prefix, and its role may read and write the items of the pool-state table whose partition key is the mock pool's name or the coursework pool's, which `tests/policy/test_pipeline.py` holds it to.
- [ ] `services/reporter/rhi_reporter/status.py` has one more description, for a verification that succeeded with that mark, which says that the mock pool's tests passed and that the GPU tests did not run for lack of capacity; `services/reporter/rhi_reporter/handler.py` reads the mark and gives the success that description.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_verify_script.py` passes: with no row written since the booking the script leaves `no-capacity`, stops its session, runs no GPU test and exits 0 when the mock run passed; with a row written and the pool not ready it runs the tests and exits with their failure.
- [ ] `uv run --locked pytest tests/unit/reporter tests/unit/test_deploy_settings.py` passes: a verification's success with the mark carries the new description, which is not the one the implement loop discharges on, and no description is longer than GitHub accepts.
- [ ] `uv run --locked pytest tests/synth/test_pipeline_verify.py tests/policy/test_pipeline.py` passes, and `tests/policy/test_pipeline.py::test_refuses_a_verification_that_may_do_more` still refuses a write whose condition names a third pool.
- [ ] The owner accepts that the verification books one coursework instance by itself after a deployment that changes a GPU pool's release, decision 14 of section 15 of the system design (needs human:owner-decision).

## References

- `docs/design/system-design.md`, sections 8.2, 8.4 and 15
- `docs/design/evidence.md`, E32
- `scripts/pipeline/verify.py`
- `scripts/pipeline/booking.py`, delivered by E6-07
- `services/reporter/rhi_reporter/status.py`
- `tests/policy/test_pipeline.py`
