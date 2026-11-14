+++
id = "E2-01"
title = "The API key format: a prefix, random characters and a checksum"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E1-02"]
+++

## Context

A student's API key is the only secret on a student's machine. The design gives it a recognisable
shape, so that scanners can find one in a homework branch, and a checksum, so that the gateway can
refuse junk before any lookup (sections 6.2 and 6.4). Only the SHA-256 of a key is ever stored. The
issuer mints keys, the gateway verifies them and the CLI stores them, so the format lives in the
package the services share.

The design fixes the prefix and the idea of a checksum, not the lengths. This issue fixes them.

## Deliverables

- [ ] `services/common/rhi_common/keys.py` defines a key as the prefix rhi_, then 40 random characters among the 62 letters and digits, then 6 characters that encode the CRC-32 of everything before them: 50 characters in all.
- [ ] It generates a key from the operating system's random source, checks the shape and the checksum of a candidate without any lookup, and gives the SHA-256 of a key, the only form that is stored.
- [ ] It publishes one regular expression that matches every key and no shorter string, for secret scanners.
- [ ] `tests/unit/common/test_keys.py` covers generation, verification and the expression.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/common/test_keys.py` passes: every generated key verifies, and a key with any one character changed fails the checksum.
- [ ] `tests/unit/common/test_keys.py::test_the_expression_matches_keys_only` shows the published expression matches each generated key in the middle of a line of text and matches no prefix of one.

## References

- `docs/design/system-design.md`, sections 6.2, 6.4
