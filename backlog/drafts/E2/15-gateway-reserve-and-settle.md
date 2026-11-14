+++
id = "E2-15"
title = "Gateway: reserve budget before a request, settle after it"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-02", "E2-67"]
+++

## Context

Checking a budget only at admission, or counting tokens only at the end, lets a client overspend by
opening many streams or by hanging up early (section 6.2, step 3). The gateway reserves, then
settles. One conditional write reserves the estimated input plus the request's output limit against
the student's daily token budget, and takes a concurrency slot with an expiring lease. A clean end
settles to the reported usage. Any other end charges the tokens observed and never less than the
input estimate.

The budget counts every input token, cached or not: an agent re-sends its whole transcript on every
request, so the default is large, 100 million tokens a day, input plus output, cached input
included, about twice what a heavy four-hour session sends (sections 11.3 and 17). It is a fairness
guard between students; the GPU-hour ceilings are the cost cap. The default concurrency limit is 4
requests (section 17). Each student's own values come with the key's lookup (issue E2-14), and the
student's record of the day is the usage item of issue E2-67.

A student without budget left is answered 402 with the cause `budget_reached`, and one at the
concurrency limit 429 with the cause `concurrency_limit` (section 6.2, step 8).

**The lease.** The design gives no length. A reservation is taken before the hold (section 6.2,
steps 3 and 5), so a lease must outlast the longest hold and then the longest request: it lasts the
pool's `hold_seconds` plus the longest request plus 60 seconds, 2,280 seconds with the defaults of
section 17 (420 + 1,800 + 60). The caller gives the two durations; issue E2-76 reads the longest
request from the gateway's settings.

The wake route (issue E2-78) and the per-student metrics (issue E2-79) need the tokens a student has
left, without reserving anything, so the ledger also answers that read.

Phase 2 makes these same two writes carry the student's affinity cookie for the pool: the
reservation returns it and the settlement stores it, so that affinity costs no extra call (sections
5.5 and 6.2, step 3). That is not built here, but this module stays the one place where a request
writes the student's record, with exactly these two writes.

The DynamoDB implementation is shown against moto, which evaluates conditional writes in process;
issue E1-16 pins it.

## Deliverables

- [ ] `services/gateway/rhi_gateway/budget.py` reserves with one conditional write to the student's usage item of issue E2-67: it adds the input estimate, computed from the request body alone before any call to the engine, plus the request's output limit to the student's reserved tokens of the day (the UTC date), and takes a concurrency slot whose lease expires, only if both the daily budget and the concurrency limit that the key's lookup gave allow; a refusal for the budget carries the cause budget_reached, and one for the limit the cause concurrency_limit.
- [ ] A clean end replaces the reservation by the reported usage, input plus output with cached input included, and frees the slot; any other end charges the tokens observed, never less than the input estimate, and frees the slot.
- [ ] A reservation is settled exactly once; a lease expires after the pool's hold_seconds plus the longest request plus 60 seconds, and a lease that expires frees its slot and charges the input estimate.
- [ ] A request makes exactly two writes to the student's record, the reservation and the settlement, and the ledger also reads, without writing, the tokens the student has left of the day.
- [ ] The ledger has a DynamoDB implementation and an in-memory one that honours the same conditions; `tests/unit/gateway/test_budget.py` covers both ends, and `tests/unit/gateway/test_budget_dynamodb.py` the DynamoDB one against moto.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_budget.py` passes: fifty reservations made at once against a budget that fits ten admit exactly ten and refuse the others with budget_reached, and a fifth concurrent request of a student whose limit is 4 is refused with concurrency_limit.
- [ ] `tests/unit/gateway/test_budget.py::test_hanging_up_early_is_charged` shows a disconnect after 100 observed tokens charges at least the input estimate, and a settle called twice charges once.
- [ ] `tests/unit/gateway/test_budget.py::test_cached_input_is_charged` settles a usage of 90,000 input tokens of which 80,000 were cached, plus 500 output tokens, and finds 90,500 charged and the tokens left lowered by as much.
- [ ] `tests/unit/gateway/test_budget.py::test_two_writes_per_request` counts the writes to the in-memory ledger for a request from reservation to settlement and finds two, and `tests/unit/gateway/test_budget.py::test_a_lease_expires_after_2280_seconds` finds a lease of the defaults holding its slot at 2,279 seconds and freed at 2,281 on a clock the test controls.
- [ ] `uv run --locked pytest tests/unit/gateway/test_budget_dynamodb.py` passes: against a usage table that moto creates with the key schema of issue E2-67, fifty reservations sent from parallel threads against a budget that fits ten admit exactly ten.

## References

- `docs/design/system-design.md`, sections 5.5, 6.2, 11.3, 12, 17
