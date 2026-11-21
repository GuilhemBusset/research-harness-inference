+++
id = "E4-46"
title = "The student guide: pointing OpenCode and Pi at the endpoint"
labels = ["size:S", "kind:docs"]
epic = 6
blocked_by = [198]
+++

## Context

The harnesses the course supports are Claude Code, Codex, an SDK script, OpenCode and Pi (section
6.1; ADR 0003, decision 4). `rhi login` writes the settings of Claude Code and Codex; for OpenCode
and Pi the student guide gives the provider entry to write (section 6.1). Issue #198 writes the
guide, `docs/student-guide.md`, and its test, `tests/unit/test_docs.py`.

Both speak OpenAI Chat Completions to an OpenAI-compatible endpoint:

- **OpenCode** takes a provider in its configuration file: the endpoint's `/v1` as the base URL, the
  model's name as the gateway's `GET /v1/models` lists it, and the key from an environment variable
  the configuration names, which the student sets from `rhi token` in the shell that starts
  OpenCode.
- **Pi** takes a provider in its `models.json`: the same base URL and model, the API kind for
  OpenAI Chat Completions, and the key from a command Pi runs when it needs it, `rhi token`, so the
  key is in no file.

Each project's own documentation is the reference for the shape of its file; the guide shows the
entry as a block a student can copy, with the endpoint on example.com, as every address of the
guide is (issue #198), and with no key, not even an invented one.

The guide also says, for these two as for the others, that the endpoint serves during sessions
only, and that outside them, or when it is down or not ready, students use their own subscriptions
(section 5.6); issue #198 writes that part for every harness, and this one does not repeat it.

## Deliverables

- [ ] `docs/student-guide.md` has a section for OpenCode and one for Pi, beside those for Claude Code and Codex, each with the provider entry to copy, how the key reaches the harness from rhi token without being written into a file, and the harness version spike S3 ran.
- [ ] `tests/unit/test_docs.py` checks that each of the two entries parses as JSON, names a base URL on example.com that ends in /v1, names no key, and that the guide's text names rhi token for each.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/test_docs.py` passes with the OpenCode and Pi sections, and fails on a guide, built in the test, whose Pi entry holds a literal key.
- [ ] A student follows each of the two sections on a laptop and completes one turn against the endpoint during a session (needs aws:deployed-environment).

## References

- `docs/design/system-design.md`, sections 5.6 and 6.1
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 4
- `client/rhi_cli/token.py`
