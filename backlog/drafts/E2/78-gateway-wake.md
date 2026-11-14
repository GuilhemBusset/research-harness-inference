+++
id = "E2-78"
title = "Gateway: the explicit wake, under the same rules as a request"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-15", "E2-16", "E2-27"]
+++

## Context

Section 5.3 gives a route that starts a pool without a request. `POST /wake`, with a key, starts a
pool under the same rules as a request (mode, budget, ceilings) without sending one, and answers at
once with the pool's status record (issue E2-27). It names the pool by its model name. `rhi wait`
calls the wake, repeats it every minute, polls the status until the pool is ready, and stops with
the cause when the pool is unavailable (issue E2-55); it is the documented way to start a homework
session on a cold pool.

An explicit wake counts as demand for 10 minutes or until the pool is ready, whichever comes first,
and as a request received at the moment the pool is ready (section 5.1); the waker of issue E2-73
keeps it. A wake on a pool unavailable for capacity is still passed to the scaler, which tries
again; for the other three immediate causes a wake asks for nothing.

The rules of a request include the budget: a student whose daily budget is spent cannot start a
GPU by waking it. The ledger's read of the tokens left (issue E2-15) answers that without reserving
anything, and an accepted wake takes no concurrency slot.

## Deliverables

- [ ] `services/gateway/rhi_gateway/local.py` answers `POST /wake`, for a valid key only, for a pool named by its model name as the body filter of issue E2-16 maps one, with that pool's status record of issue E2-27 at once.
- [ ] The wake asks for nothing for a pool unavailable with pool_disabled, pool_ceiling_reached or pool_scheduled_only, and for any other pool, one unavailable for capacity included, asks the scaler for a wake through the waker of issue E2-73 and is recorded there as an explicit wake.
- [ ] A wake from a student who has no tokens left of the day, by the ledger's read of issue E2-15, is refused with the cause budget_reached and asks for nothing; an accepted wake reserves no tokens and takes no concurrency slot.
- [ ] `services/gateway/rhi_gateway/app.py` serves the wake from this module, and `tests/unit/gateway/test_wake.py` covers it.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_wake.py` passes: a wake without a key is refused with the cause invalid_key, and a wake for an enabled cold pool makes one wake call and one pending wake and answers that pool's record.
- [ ] `tests/unit/gateway/test_wake.py::test_stopped_pools_are_not_woken` shows no wake call, and an answer that reads unavailable with its cause, for a disabled pool, a scheduled-only pool that nothing has started and a budget-latched pool.
- [ ] `tests/unit/gateway/test_wake.py::test_a_spent_budget_wakes_nothing` shows a 402 with the cause budget_reached and no wake call for a student with no tokens left, and no reservation and no slot taken for an accepted wake.
- [ ] `tests/unit/gateway/test_wake.py::test_no_capacity_still_wakes` shows a wake for a pool unavailable with capacity_unavailable asking the scaler once.

## References

- `docs/design/system-design.md`, sections 5.1, 5.3, 6.2
