+++
id = "E4-48"
title = "Gateway: take and release the concurrency slot with no token budget, reservation or charge"
labels = ["size:M", "kind:app"]
epic = 6
+++

## Context

A request takes a concurrency slot with an expiring lease and its end releases it; the same two
writes carry the student's affinity cookie for the pool. Nothing is reserved or charged: there is no
per-student token budget (section 6.2, step 3; ADR 0003, decisions 2 and 3). Phase 1's ledger
(`services/gateway/rhi_gateway/budget.py`) still reserves an estimate of the input plus the output
limit against a daily budget, charges the usage at settlement, refuses with budget_reached, and
feeds a gauge of the tokens left. This issue removes that from the gateway and keeps everything that
shares its writes.

**What stays, with the rules the module's docstring gives them:** the lease and its expiry
(`lease_seconds`), the sweep of expired leases, the slot condition and its refusal
concurrency_limit, the midnight stand-ins that keep a request's slot across midnight, the affinity
cookie, the rebalance number, the withdrawal, and both ledgers. The usage item stays one per student
and UTC day: changing its key is not part of this change. A token count, like every request, is
charged nothing. Who answers a request and with what is unchanged but for the refusal for a budget.

**What goes:** the input estimate (`BYTES_PER_TOKEN`, `estimate_input_tokens`), the output limit
given to the reservation (`output_limit` in `services/gateway/rhi_gateway/app.py`), the daily budget
in the reservation and its DynamoDB condition, the charge at settlement and its token arguments, the
read of the tokens left (`tokens_left`), the refusal of a wake for a spent budget in
`services/gateway/rhi_gateway/local.py`, and the gauge rhi_gateway_student_budget_left_tokens with
the ledger the metrics are given for it (`services/gateway/rhi_gateway/metrics.py`,
`services/gateway/rhi_gateway/main.py`), which no dashboard or smoke test reads. The record of
`services/gateway/rhi_gateway/telemetry.py` carries the student's key record only for that gauge.
The cause budget_reached stays in `services/common/rhi_common/causes.py` for spike S2's job, which a
later issue (E4-49) changes, but nothing in the gateway produces it any more.

**The rollout.** A gateway deployment replaces tasks one by one, so tasks of the release before and
of this one share the usage table for a while, and the release before refuses a day or a lease that
lacks an attribute it reads (`services/common/rhi_common/students.py`: a day's `tokens`, a lease's
`tokens` and `estimate`). So this release still writes them, as 0, on each day it opens and each
lease it adds. And so that a later change (E4-50) can stop writing them, and stop writing a key's and
a roster entry's daily_token_budget, this release reads a day, a lease, a key and a roster entry
that lack them. The issuer deploys before the gateway, so the gateway must read such items before
any issuer writes them.

## Deliverables

- [ ] `services/gateway/rhi_gateway/budget.py` takes a request's lease under the concurrency limit alone: no input estimate, output limit or daily budget takes part in the reservation or in its condition, and its only refusal is concurrency_limit.
- [ ] Its settlement removes the lease and charges nothing whatever the request's end, it has no read of the tokens left, and its docstring describes the lease, midnight, the cookie and the rebalance number with no budget.
- [ ] Each day it opens holds tokens 0 and each lease it adds holds tokens 0 and estimate 0, and a lease written by the release before, with its tokens and estimate, is settled and swept like any other.
- [ ] `services/common/rhi_common/students.py` reads a key and a roster entry that lack daily_token_budget, and a day and a lease that lack tokens or estimate, and still writes all four as before.
- [ ] `services/gateway/rhi_gateway/app.py` reserves with neither the body nor an output limit, `services/gateway/rhi_gateway/local.py` refuses no wake for a budget and is given no ledger, and `services/gateway/rhi_gateway/main.py` gives the metrics no ledger.
- [ ] `services/gateway/rhi_gateway/metrics.py` no longer serves rhi_gateway_student_budget_left_tokens, and the record of `services/gateway/rhi_gateway/telemetry.py` no longer carries the student's key record.
- [ ] `tests/unit/gateway/test_budget.py` and `tests/unit/gateway/test_budget_dynamodb.py` keep their tests of the slot, the lease, midnight, the cookie, the rebalance number and the withdrawal and lose those of the budget; `tests/unit/common/test_students.py` covers the reads of items without the budget's attributes; `tests/unit/gateway/test_student_metrics.py`, `tests/unit/gateway/test_experience.py`, `tests/unit/gateway/test_wake.py`, `tests/unit/gateway/test_local.py`, `tests/unit/gateway/test_main.py` and `tests/replay/test_replay.py` follow.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_budget.py tests/unit/gateway/test_budget_dynamodb.py` passes: a student whose key carries a daily budget of 0 is admitted up to the concurrency limit and refused beyond it with concurrency_limit; a settlement frees its slot and leaves the day's tokens as they were; a lease the release before wrote, with tokens and an estimate, is settled and swept; and each day and lease written holds tokens 0 and estimate 0.
- [ ] `uv run --locked pytest tests/unit/common/test_students.py` passes: a key, a roster entry, a day and a lease written without daily_token_budget, tokens or estimate each read back, and every item is still written with them.
- [ ] `uv run --locked pytest tests/unit/gateway tests/unit/common tests/replay tests/smoke/test_smoke.py` passes.
- [ ] `grep -rnE "BUDGET_REACHED|BUDGET_LEFT|tokens_left|estimate_input_tokens|output_limit" services/gateway` prints nothing.
- [ ] During and after the gateway's deployment of this change, the pipeline's verification passes and the gateway's log holds no record with the cause none (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.5, 6.2 and 14
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decisions 2 and 3
- `services/gateway/rhi_gateway/budget.py`
- `services/common/rhi_common/students.py`
