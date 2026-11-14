+++
id = "E0-04"
title = "Redact everything the engine publishes with the hygiene patterns"
labels = ["size:M", "kind:factory", "loop:infra"]
epic = "E0"
+++

## Context

The engine publishes on GitHub: evidence comments with the tail of a command's output, pull request
bodies, fault issues, follow-up issues, the log of a failed CI job. A command's output can carry an
account ID or an ARN that no source file holds, for example from a synthesized template. ADR 0001
lists redaction of published evidence among its follow-ups. Today the engine's own `redact` in
`implement_loop/engine.py` replaces the repository's path and the home directory only, and only in a
fault's traceback.

The hygiene scanner already has what is needed: `scan.redact(text)` in `tools/hygiene/scan.py`, and
`python3 tools/hygiene/scan.py --redact`, copy a text with every finding masked. The engine runs
outside the sandbox, as the owner, so the scanner it uses must be the one of the checkout the engine
runs from, never a copy in an issue's worktree, which a builder can write. Run as the owner, the
scanner also masks the owner's local values where their list exists (`tools/hygiene/README.md`,
"Values only the owner knows").

Ticking an issue's checkboxes sends back the issue's own text with ticks and evidence links; it is
not redacted, so that the engine never rewrites what the owner wrote.

A scanner that cannot run is treated like GitHub failing: the issue waits with the existing kind
github, which the supervisor already resumes, rather than with a new kind the engine's state machine
would have to learn.

This issue changes the engine, a protected path (ADR 0001, decision 10); the builder may change the
files named below because this issue names them.

## Deliverables

- [ ] `tools/implementation-loop/implement_loop/redact.py` passes a text through the redaction of the hygiene scanner of the checkout the engine runs from, and returns the masked text.
- [ ] Every title and body the engine and the supervisor send GitHub to create a comment, a pull request or an issue, or to update a comment or a pull request body, passes through it first, through `tools/implementation-loop/implement_loop/github.py` and `tools/implementation-loop/implement_loop/cli.py`.
- [ ] When the scanner is missing or fails, nothing is published, the issue waits with the existing kind github (WAIT_GITHUB of the engine's model module), which the supervisor resumes, and raw text is never sent as a fallback.
- [ ] `tools/implementation-loop/UPSTREAM.md` records the patch, and `tools/implementation-loop/tests/test_redaction.py` covers it.

## Acceptance criteria

- [ ] `scripts/check.sh engine-tests` exits 0, with `tools/implementation-loop/tests/test_redaction.py` showing that a 12-digit account number and an ARN, both built in the test, reach the fake GitHub masked in a comment, a pull request body, a fault issue and a follow-up issue.
- [ ] `tools/implementation-loop/tests/test_redaction.py::test_no_scanner_no_publication` shows that with the scanner removed the engine publishes nothing and leaves the issue waiting with the kind github.
- [ ] `tools/implementation-loop/tests/test_redaction.py::test_the_worktree_scanner_is_never_run` gives an issue's worktree a scanner that masks nothing and finds the published text still masked.

## References

- `docs/adr/0001-software-factory.md`, decision 4, Consequences
- `docs/design/system-design.md`, section 10
- `tools/hygiene/README.md`
