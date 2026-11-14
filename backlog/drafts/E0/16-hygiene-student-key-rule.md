+++
id = "E0-16"
title = "Hygiene scanner: recognise a student API key"
labels = ["size:S", "kind:factory", "loop:infra"]
epic = "E0"
blocked_by = ["E2-01"]
+++

## Context

A student's key looks like `rhi_`, random characters and a checksum, so that scanners can recognise
one in a homework branch (`docs/design/system-design.md`, section 6.4). Issue E2-01 fixes the
format: the prefix, 40 random letters and digits, and 6 characters of checksum, 50 characters in
all. The hygiene scanner's own documentation asks for the pattern in its rule `token` as soon as the
format is final (`tools/hygiene/README.md`, "Rules"). With it, a key pasted into a test, a capture,
an issue or a commit message is stopped at commit, in the loop's verifier and in CI, and is masked
in what the engine publishes.

The scanner is standard-library Python and imports nothing from the product, so it holds its own
copy of the pattern; a test keeps that copy equal to the format of issue E2-01. The values the test
needs are generated at run time, so that the test file itself scans clean.

**What this means for every other issue.** Once this rule lands, a literal key anywhere in the tree,
in a test, a fixture or a replayed capture, fails `scripts/check.sh hygiene`, the hooks, the loop's
verifier and CI, and this issue's own acceptance item on the tree as it stands. So no issue writes a
key as a literal: a test or a capture that needs one generates it at run time with the key module of
issue E2-01, or carries a value that is not a key (one character short, say). The issues that handle
keys (the gateway's authentication, the issuer, the CLI, the captures) are drafted that way, so the
order in which they and this issue land does not matter.

This issue changes `tools/hygiene/`, a protected path on the safety surface (ADR 0001, decision 10);
the builder may change the files named below because this issue names them. The allow-list
`hygiene.toml` is not changed.

## Deliverables

- [ ] `tools/hygiene/scan.py` reports, under the rule token, a student key as the format of issue E2-01 defines it (the prefix rhi_ and 46 characters), anywhere in a line, when no further letter or digit follows it.
- [ ] The redaction mode masks such a key like any other finding.
- [ ] `tools/hygiene/README.md` lists the key in the row of the rule token and no longer asks for the pattern to be added.
- [ ] `tools/hygiene/tests/test_student_key.py` covers the rule with keys generated at run time by the key module of issue E2-01, and holds no key itself.

## Acceptance criteria

- [ ] `uv run --locked pytest tools/hygiene/tests/test_student_key.py` passes: every generated key is reported as token in the middle of a line of text and in a file name, and comes out of the redaction masked.
- [ ] `tools/hygiene/tests/test_student_key.py::test_look_alikes_stay_quiet` shows no finding for a key one character short, for the bare prefix, and for the package names of this repository that start with the prefix.
- [ ] `scripts/check.sh hygiene` exits 0 on the tree as it stands.

## References

- `docs/design/system-design.md`, sections 6.4, 10
- `tools/hygiene/README.md`
- `docs/adr/0001-software-factory.md`, decisions 4 and 10
