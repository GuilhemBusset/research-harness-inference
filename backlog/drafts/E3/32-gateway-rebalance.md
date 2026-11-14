+++
id = "E3-32"
title = "Gateway: for each new rebalance number, drop the cookie of about one student in ten"
labels = ["size:M", "kind:app"]
epic = "E3"
blocked_by = ["E2-73", "E3-28", "E3-30"]
+++

## Context

The scaler raises a pool's rebalance number while the pool is lopsided (issue E3-31). For each new
number the gateway drops the cookie of about one student in ten, chosen by a hash of the student and
the number. The student's record keeps the last number applied, so each number acts once.
Least-outstanding routing then sends that student's next request to the emptiest instance, where it
pays for its transcript once. The gateway cannot aim: the cookie is opaque, so it does not know which
instance a student is on, and a student dropped from the emptier instance simply returns to it
(section 5.5).

The share is the pool's setting, about one in ten by default (section 17; issue E3-28). The number
arrives with the scaler's note on the pool, which each gateway task reads every 5 seconds and keeps
(issue E2-73). The cookie and the student's record are issue E3-30's: the reservation returns them
and the settlement stores them, in the two writes a request makes (issue E2-15). The last number
applied rides on the same two writes. It is an attribute of the day of usage of
`services/common/rhi_common/students.py` (issue E2-67), which lists every attribute the item may
hold; the design names none, and this issue calls it rebalance_applied, a map from a pool's name to
the last rebalance number applied.

A student who sent nothing while the number rose several times is judged once, on the number the
gateway holds when the student's next request arrives: the numbers in between belonged to an
imbalance that student was not part of. The choice depends on the student and the number only, so
every gateway task makes the same one.

## Deliverables

- [ ] `services/gateway/rhi_gateway/readiness.py` gives, for each pool, the rebalance number of the scaler's note it last read.
- [ ] `services/gateway/rhi_gateway/affinity.py` drops the student's cookie for a request when the pool's rebalance number differs from the last number the student's record has applied for that pool and a hash of the student and that number selects the student, which it does for the pool's share of students.
- [ ] `services/gateway/rhi_gateway/budget.py` keeps on the student's record the last rebalance number applied per pool: the reservation returns it, and the settlement stores the pool's number the request was judged on, whether or not its cookie was dropped.
- [ ] `services/common/rhi_common/students.py` adds to the day of usage the attribute rebalance_applied, a map from a pool's name to a whole number, absent until written, and lists it among the item's attributes; `tests/unit/common/test_students.py` covers its round trip.
- [ ] `tests/unit/gateway/test_rebalance.py` covers the choice and its record.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_rebalance.py` passes: over 10,000 students holding a cookie, one new number with the default share drops between 9 and 11 percent of the cookies, and the same number drops none on their next request.
- [ ] `tests/unit/gateway/test_rebalance.py::test_the_choice_is_shared` finds two gateway instances in the test making the same choice for the same student and number, and two numbers selecting different sets of students.
- [ ] `tests/unit/gateway/test_rebalance.py::test_an_unchanged_number_keeps_the_cookie` finds every student's cookie sent while the pool's number equals the one their record has applied.
- [ ] `tests/unit/gateway/test_budget.py::test_two_writes_per_request` still passes with the number carried, and `tests/unit/common/test_students.py::test_attributes_are_the_listed_ones` accepts rebalance_applied.

## References

- `docs/design/system-design.md`, sections 5.1, 5.5, 6.2, 17
