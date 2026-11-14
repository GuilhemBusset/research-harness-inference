+++
id = "E2-51"
title = "Key issuer: one key per installation, at most two installations, and a daily limit"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-50"]
+++

## Context

The details of section 6.4 that decide what a leaked or phished key is worth. A student has one key
per installation of the CLI, and at most two installations (a laptop and a desktop, say). An
installation is an identifier the CLI creates at its first login and sends with the device code
(issue E2-53). `rhi login` on an installation replaces that installation's key; the old one stops
working within 60 seconds, which is the gateway's lookup cache (issue E2-14). Keys expire at course
end; the expiry is set when a key is minted (issue E2-50) and enforced by the gateway (issue E2-14).

Issuances per student are limited, to 5 a day by default (section 17), counted over the UTC day
like the token budget. The limit reaches the function as the environment variable
RHI_ISSUANCES_PER_DAY, 5 when unset; issue E2-52 sets it from the configuration key
`key_issuances_per_day`. Revocation, by a key or by a fresh device flow, is issue E2-83.

The student's keys are found through the keys table's index by student, and the installation is an
attribute of the key item (issue E2-67). Keys are generated at run time in the tests: a literal key
in a test file fails the hygiene scan (issue E0-16).

## Deliverables

- [ ] `services/issuer/rhi_issuer/rules.py` applies the rules on each issuance: the installation's identifier arrives with the device code, and the key previously issued to the same installation is revoked in the same write that stores the new one.
- [ ] A student has active keys for at most two installations; a login from a third is refused with a fixed message that names the command rhi revoke --all.
- [ ] A student's issuances are limited per UTC day by the number the function is given, 5 by default, and a refusal for that reason is a fixed message.
- [ ] `tests/unit/issuer/test_rules.py` covers each rule against tables that moto creates, with every key generated at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/issuer/test_rules.py` passes: a second login from the same installation leaves that installation one active key, a second installation gets its own key, a third is refused, and a sixth issuance in one day is refused.
- [ ] `tests/unit/issuer/test_rules.py::test_replacement_is_one_write` shows no state of the store in which the new key exists and the old key of the same installation is not revoked.

## References

- `docs/design/system-design.md`, sections 6.4, 17
