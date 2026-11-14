+++
id = "E2-83"
title = "Key issuer: a key revokes itself, and a fresh device flow revokes every key"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-51"]
+++

## Context

`rhi revoke` revokes the installation's own key, authenticated by that key. `rhi revoke --all`
revokes every key of the student and requires a fresh device flow, since it must work when a key is
lost (section 6.4). The issuer's routes all live under `/auth/`. The design does not name these two;
they are `POST /auth/revoke`, with the key in the `Authorization: Bearer` header, and
`POST /auth/revoke-all`, with a device code, which the CLI calls (issue E2-55).

A revoked key stops working once the gateway's 60-second lookup cache has expired (issue E2-14),
because a revocation marks the key item of issue E2-67 revoked, which is what the gateway reads.
Keys are generated at run time in the tests: a literal key in a test file fails the hygiene scan
(issue E0-16).

## Deliverables

- [ ] `services/issuer/rhi_issuer/handler.py` serves `POST /auth/revoke`, authenticated by a key in the Authorization header, which revokes that key and no other.
- [ ] It serves `POST /auth/revoke-all`, which takes a device code, redeems it and checks it against the roster as a login's is (issue E2-50), revokes every key of that student, and accepts no key in place of the device code.
- [ ] `services/issuer/rhi_issuer/revoke.py` revokes each key by one write that marks its key item revoked; a key that is malformed, unknown or already revoked is refused with a fixed message and changes nothing.
- [ ] `tests/unit/issuer/test_revoke.py` covers both routes against tables that moto creates, with every key generated at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/issuer/test_revoke.py` passes, and `tests/unit/issuer/test_revoke.py::test_a_key_revokes_itself_only` revokes one key with that key and leaves the student's other key active.
- [ ] `tests/unit/issuer/test_revoke.py::test_revoking_all_needs_a_device_flow` shows every key of the student revoked after a redeemed device code, with no key presented, the same request refused when it presents a key instead of a device code, and a third installation accepted afterwards.

## References

- `docs/design/system-design.md`, section 6.4
