+++
id = "E6-09"
title = "Weekly canary: book its instance, and report no capacity as its own result"
labels = ["size:M", "kind:infra"]
epic = "E6"
blocked_by = ["E6-08"]
+++

## Context

The weekly GPU canary runs the coursework pool's smoke tests against what is deployed, whatever was
deployed (system design, section 8.2, step 6). Today its first stream wakes the pool, and the job
measures the wake. With sessions it does what the verification does (E6-07, E6-08): it books one
coursework instance with `scripts/pipeline/booking.py`, under the name `pipeline/canary`, waits
`check_wait_minutes` for it, runs the tests, and stops the session it booked. It books only when
the pool has no session, and on a pool someone else has booked it tests the instances that are
there and stops nothing.

It reports one of three results. The measurement `gpu-canary` carries a verdict and named numbers,
never text (`rhi_reporter.measure`), so the third result is the verdict of a run that could not
measure, with a number that says why:

| Result | Verdict | Numbers |
| --- | --- | --- |
| The tests ran and passed | pass | `wait_seconds`, `smoke_tests_passed`, `smoke_tests_failed`, `no_capacity` 0 |
| A test failed, or an instance was launched and never served | fail | the same |
| No instance was launched during the wait | error | `wait_seconds`, `no_capacity` 1 |

`wait_seconds` replaces `wake_seconds`: the seconds from the booking to the pool's first `ready`,
and 0 on a pool that was ready when the job began. "No instance was launched" is read as in E6-08,
from the heartbeat rows of the coursework pool, which the canary's role may already query.

The job finds the pool-state table as the deployment hold does, by its logical ID in the pools
stack. Its role gains that read, and a read and a write of the coursework pool's items in the
pool-state table and of no other pool's (`dynamodb:LeadingKeys`). `src/rhi/pipeline/` and
`tests/policy/` are protected paths (ADR 0001, decision 10): this issue names the files it changes
there, and the policy test must admit those three grants on the canary's role and still refuse any
other.

After this issue the canary no longer depends on a wake, and a week without capacity is a run that
did not measure, not a failed canary.

## Deliverables

- [ ] `scripts/pipeline/canary_result.py` books the coursework pool with the verification's booking module when it has no session, waits until the status reads the pool ready or `check_wait_minutes` have passed, runs the smoke tests, and stops the session it booked.
- [ ] Its measurement has the numbers `wait_seconds`, `smoke_tests_passed`, `smoke_tests_failed` and `no_capacity`, and the three results of the table in Context; the number `wake_seconds` is gone.
- [ ] `src/rhi/pipeline/canary.py` gives the job the pools stack's name, the pool-state table's logical ID, `check_wait_minutes` and `session_max_hours`, and its role may describe that one stack resource and read and write the items of the pool-state table whose partition key is the coursework pool's name.
- [ ] `tests/policy/test_pipeline.py` holds the canary's role to those grants beside the ones it has.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_canary_result.py` passes: each of the three results gives its verdict and its numbers, the session is stopped after a failure too, and a pool that already has a session is neither booked nor stopped.
- [ ] `uv run --locked pytest tests/synth/test_canary.py tests/policy/test_pipeline.py` passes, and `tests/policy/test_pipeline.py::test_each_rule_on_the_canary_refuses_its_breach` refuses a canary whose role may write another pool's items.
- [ ] In the account, a run of the canary's job books one coursework instance, posts `measure/gpu-canary` with the verdict pass, and leaves the pool with no active session (needs gpu:capacity).

## References

- `docs/design/system-design.md`, section 8.2
- `scripts/pipeline/canary_result.py`
- `src/rhi/pipeline/canary.py`
- `scripts/pipeline/booking.py`, delivered by E6-07
- `tests/policy/test_pipeline.py`
