+++
id = "E6-07"
title = "Verification: book the mock pool for the smoke tests"
labels = ["size:M", "kind:infra"]
epic = "E6"
blocked_by = ["E6-02"]
+++

## Context

After every deployment the pipeline's verification runs the smoke tests on the mock pool. Today
the pool comes up because the first stream wakes it. With sessions, the step books a session of one
instance on the mock pool, runs the tests, and stops the session (system design, section 8.2, step
6): a session booked, an instance serving, the session stopped is the whole control loop, exercised
on every deployment (section 8.5).

This issue adds the booking and nothing else of step 6. The gateway still holds and wakes until
E6-18, and it serves the canary's key whatever the session until E6-17, so the smoke tests
themselves do not change: on a pool the step has booked, the first stream finds the pool ready. The
coursework pool is booked in E6-08, and "no capacity" gets its own outcome there.

The booking is a small module of its own, because the canary (E6-09) and the spike jobs of the
mock pool (E6-12, E6-13) book the same way. It writes with the rules of `rhi_common.sessions`, one
instance, under the name `pipeline/verify`, for twice the wait below: the wait, and as long again
for the tests, so that a step that dies leaves a session that ends by itself. The design gives no
length for that booking; this issue does. It books only a pool that has no session booked or
active, and it stops only the session it booked: a session someone else booked meanwhile is left
alone, and the tests run on the instances that are there.

The step finds the pool-state table as the deployment hold does, by its logical ID in the pools
stack (`src/rhi/pipeline/hold.py`). Its role gains that read, and a read and a write of the mock
pool's items in that table and of no other pool's: the condition is on the item's partition key
(`dynamodb:LeadingKeys`), in the form the canary's role already has for the heartbeats table. That
condition covers every record of the mock pool's partition, not only its session: a table of
sessions would be a new stateful resource.

`src/rhi/pipeline/` and `tests/policy/` are protected paths (ADR 0001, decision 10). This issue
names the files it changes there: the verification's role is held, grant by grant, by
`tests/policy/test_pipeline.py`, which must admit the three new grants and still refuse any other.

How long a check waits for the instance of a session it booked is a configuration key,
`check_wait_minutes`, 45 when left out: the wait the design gives for a GPU instance (section 17).
The design gives none for the mock pool, and this issue uses the same.

After this issue the deployed verification no longer depends on a wake. It must not assert that the
mock instance leaves when the session is stopped: until E6-15 the idle rule takes it, after
`idle_minutes`.

## Deliverables

- [ ] `scripts/pipeline/booking.py` books one instance on a pool for a number of minutes, under a name it is given, only when the pool has no session booked or active, and stops a session only while the item still reads as the booking it made.
- [ ] `scripts/pipeline/verify.py` books the mock pool for twice `check_wait_minutes` before its run of the smoke tests, waits until the gateway's status reads the pool ready or `check_wait_minutes` have passed, runs the tests, and stops the session it booked whatever the tests' result.
- [ ] `src/rhi/config.py` reads the key `check_wait_minutes`, a whole number of at least 1 that is 45 when left out, and `config.example.toml` holds it.
- [ ] `src/rhi/pipeline/verify.py` gives the step the pools stack's name, the pool-state table's logical ID, `check_wait_minutes` and `session_max_hours`, and its role may describe that one stack resource and read and write the items of the pool-state table whose partition key is the mock pool's name; `tests/policy/test_pipeline.py` holds the role to those grants beside the ones it has, and refuses a read or a write of another pool's items, of every item, or of another table.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_booking.py tests/unit/test_verify_script.py` passes: the mock pool is booked before the tests and stopped after them, also when they fail; a pool that already has a session is neither booked nor stopped; and the tests start once the status reads ready or the wait is over.
- [ ] `tests/policy/test_pipeline.py::test_refuses_a_verification_that_may_do_more` refuses a verification whose role may write the coursework pool's items, and one whose write names no partition key.
- [ ] `uv run --locked pytest tests/synth/test_pipeline_verify.py tests/policy/test_pipeline.py tests/unit/test_config.py` passes on the app as it stands.
- [ ] In the pipeline, `tests/smoke/test_smoke.py` passes on the mock pool in a session the step booked and stopped (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 8.2, 8.5 and 17
- `scripts/pipeline/verify.py`
- `src/rhi/pipeline/verify.py`
- `src/rhi/pipeline/hold.py`
- `tests/policy/test_pipeline.py`
- `services/common/rhi_common/sessions.py`, delivered by E6-01
