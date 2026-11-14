+++
id = "E3-30"
title = "Gateway: keep each student's load-balancer cookie per pool on the budget record, and never pass it to the student"
labels = ["size:M", "kind:app"]
epic = "E3"
blocked_by = ["E2-15", "E2-24", "E2-67", "E2-77"]
+++

## Context

The gateway is the internal load balancer's client, so the gateway keeps the stickiness cookie: one
per student and pool, on the student's budget record. The write that reserves budget returns it and
the write that settles stores it (section 6.2, step 3), so affinity costs no extra call and every
gateway task sees the same value. The cookie never reaches the student (section 5.5).

The load balancer's cookie for duration-based stickiness is named `AWSALB`; it is generated and
encrypted by the load balancer, its expiry is reset after every request, and a cookie that cannot be
decoded or names a target that left gets a new target and an updated cookie (evidence E13). The
gateway therefore treats it as opaque: it does not know which instance a student is on.

The budget module makes exactly two writes to the student's record per request, the reservation and
the settlement (issue E2-15), and the proxy forwards each request to the pool's upstream through the
internal load balancer (issue E2-24), and every end of a request is settled in one place (issue
E2-77). This issue adds the cookie to those two writes and to the proxy, and no write of its own.

**The attribute.** The student's record is the day of usage of `services/common/rhi_common/students.py`
(issue E2-67), whose test refuses an attribute outside its listed ones, and which says that phase 2
adds the cookie, absent until written. The design names no attribute; this issue calls it
affinity_cookies, a map from a pool's name to the cookie's value as the load balancer set it. A student's parallel requests, such as Claude Code's subagents,
follow the same cookie (section 5.5). Moving a few students when a pool is lopsided is issue E3-32.

## Deliverables

- [ ] `services/gateway/rhi_gateway/budget.py` keeps on the student's record one load-balancer cookie per pool: the reservation's write returns the one held for the request's pool, and the settlement's write stores the one the request ended with; no other write touches it.
- [ ] `services/gateway/rhi_gateway/affinity.py` gives the proxy the cookie to send for a request, and takes from the internal load balancer's answer the AWSALB cookie it sets; when the answer sets none, the request ends with the cookie it was sent.
- [ ] `services/gateway/rhi_gateway/proxy.py` sends the student's cookie for the pool to the internal load balancer, sends none when the record holds none, and passes to the client no Set-Cookie header and no load-balancer cookie.
- [ ] `services/common/rhi_common/students.py` adds to the day of usage the attribute affinity_cookies, a map from a pool's name to a cookie, absent until written, and lists it among the item's attributes; `tests/unit/common/test_students.py` covers its round trip.
- [ ] `tests/unit/gateway/test_affinity.py` covers the cookie's path with the in-memory ledger and an upstream built in the test.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_affinity.py` passes: a student's second request to a pool carries the cookie the first answer set, a request of that student to another pool carries none of it, and two students never share one.
- [ ] `tests/unit/gateway/test_affinity.py::test_the_cookie_never_reaches_the_student` finds no Set-Cookie header and no AWSALB value in any answer to the client, streamed and not, in each wire format.
- [ ] `tests/unit/gateway/test_budget.py::test_two_writes_per_request` still passes with the cookie carried: a request still makes two writes to the student's record.
- [ ] `uv run --locked pytest tests/unit/common/test_students.py` passes: a day of usage with two pools' cookies survives a round trip through its item form, one without reads as none, and `tests/unit/common/test_students.py::test_attributes_are_the_listed_ones` accepts affinity_cookies.

## References

- `docs/design/system-design.md`, sections 5.5, 6.2
- `docs/design/evidence.md`, E13
