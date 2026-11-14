+++
id = "E2-14"
title = "Gateway: authenticate from the headers, before reading the body"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E1-16", "E2-01", "E2-13", "E2-67"]
+++

## Context

The key arrives in `Authorization: Bearer` or in `x-api-key`; harnesses differ (evidence E15). The
gateway authenticates from the headers before it reads the body, verifies the key's checksum first
so that junk costs nothing, then looks up its SHA-256, and caches the answer for 60 seconds (section
6.2, step 2). That cache is the bound of section 6.4: a key that was replaced or revoked stops
working within 60 seconds. A key that is missing, malformed, unknown, revoked or expired, and a
request whose two key headers differ, are a 401 with the cause `invalid_key` (section 6.2, step 8).

**One read answers a request.** The key item of issue E2-67 carries everything the gateway needs:
the student's GitHub ID, which addresses their usage item (issue E2-15) and is never logged or
labelled (section 6.6), their alias, daily token budget and concurrency limit, the key's expiry and
whether it is revoked. The issuer copies the budget and the limit from the student's roster entry
when it mints the key (issue E2-50), and the admin CLI writes a changed budget or limit into the
student's active keys (issue E2-56). So the lookup is one read of the keys table, as section 6.2
describes it, and the gateway never reads the roster.

The DynamoDB implementation is shown against moto, which evaluates DynamoDB's key schemas and
conditions in process with no account; issue E1-16 pins it. Keys the tests need are generated at run
time with the key module of issue E2-01: a literal key in a test file fails the hygiene scan once
issue E0-16 has landed.

## Deliverables

- [ ] `services/gateway/rhi_gateway/auth.py` reads the key from either header; a request with both headers and different keys, or with neither, is refused with the cause invalid_key of issue E2-02.
- [ ] A key whose shape or checksum is wrong is refused with no lookup; a well-formed key is looked up by its SHA-256, the answer, found or not, is cached for 60 seconds, and a key that is unknown, revoked or past its expiry is refused with the same cause, which never says which.
- [ ] The body is not read, and no budget is touched, before the key is accepted.
- [ ] `services/gateway/rhi_gateway/keystore.py` is the lookup: a DynamoDB implementation that reads the key item of issue E2-67 from the keys table, and an in-memory one for tests, both returning the student's GitHub ID, alias, daily token budget, concurrency limit, the key's expiry and whether it is revoked.
- [ ] `tests/unit/gateway/test_auth.py` covers the rules and `tests/unit/gateway/test_keystore.py` the DynamoDB lookup, with every key generated at run time and none written in either file.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_auth.py` passes: each header is accepted, differing headers are refused with a 401 and the cause invalid_key, and a malformed key makes no lookup.
- [ ] `tests/unit/gateway/test_auth.py::test_the_body_is_not_read` sends a refused request whose body raises when read, and the body is never read.
- [ ] `tests/unit/gateway/test_auth.py::test_the_cache_lasts_60_seconds` shows one lookup for two requests 59 seconds apart and two lookups at 61, with a clock the test controls.
- [ ] `tests/unit/gateway/test_auth.py::test_a_revoked_key_stops_within_60_seconds` revokes a key in the store just after a request accepted it, and a request 61 seconds later is refused.
- [ ] `uv run --locked pytest tests/unit/gateway/test_keystore.py` passes: against a keys table that moto creates with the key schema of issue E2-67, a key item written there is read back with its alias, budget, limit and expiry, and an unknown, a revoked and an expired key give the same answers as the in-memory store.

## References

- `docs/design/system-design.md`, sections 6.2, 6.4
- `docs/design/evidence.md`, E15
