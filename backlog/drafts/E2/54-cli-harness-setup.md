+++
id = "E2-54"
title = "The rhi CLI: rhi login points Claude Code and Codex at the endpoint through a key helper"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-27", "E2-53"]
+++

## Context

Once it holds the key, `rhi login` configures each harness to fetch it through a helper, so that
the key is not pasted into settings files that get committed (section 6.4, step 5): `apiKeyHelper`
for Claude Code, an auth command for Codex (evidence E15). Both call `rhi token`, which prints the
stored key.

Codex's idle timer counts parsed events and ignores SSE comments, with a default of 300 seconds, so
a Codex without a profile fails a long hold at 5 minutes: `rhi login` writes a Codex provider
profile with a 15-minute stream timeout (section 5.3; evidence E12). Codex accepts the Responses
format only (evidence E15). Claude Code sends `claude-*` model names, which the gateway maps to the
default pool (section 6.2, step 4); Codex sends the model its profile names, so the profile names
the pool that the gateway's `GET /v1/models` marks as the default (issue E2-27), read with the new
key.

Section 4.7 lists a smaller compaction window for Claude Code as a lever that `rhi login` can
write. The design does not settle it or its value, and the setting's name is not verified (evidence
E15): it is not part of this issue.

The command dispatcher is `client/rhi_cli/main.py` (issue E2-53). The stored key the tests search
for is generated at run time: a literal key in a test file fails the hygiene scan (issue E0-16).

## Deliverables

- [ ] `client/rhi_cli/token.py` adds the command rhi token, which prints the stored key and nothing else, for helpers to call, and `client/rhi_cli/main.py` dispatches it.
- [ ] `client/rhi_cli/harness.py` writes Claude Code's user settings so that it reaches the endpoint and obtains its key from the helper rhi token.
- [ ] It writes a Codex provider profile for the endpoint that uses the Responses format, obtains its key with the auth command rhi token, has a stream idle timeout of 900000 milliseconds, and names as its model the default pool's model name, as `GET /v1/models` gives it.
- [ ] `client/rhi_cli/login.py` runs both once the key is stored; no file they write holds the key; settings they do not own are left as they were; running the login twice changes no setting the second time; the command prints each file it changed.
- [ ] `tests/unit/client/test_harness.py` covers both harnesses in scratch home directories, with a stub of the gateway's `GET /v1/models` and a key generated at run time.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/client/test_harness.py` passes: after a login, Claude Code's settings name the helper and the endpoint, Codex's profile has the 900000 millisecond timeout, the Responses format, the auth command and the default pool's model, and other settings in both files are untouched.
- [ ] `tests/unit/client/test_harness.py::test_the_key_is_in_no_file` searches every file the login wrote for the stored key and finds none.
- [ ] `tests/unit/client/test_harness.py::test_a_second_login_changes_no_setting` logs in twice and finds both settings files byte for byte the same after the second login.

## References

- `docs/design/system-design.md`, sections 4.7, 5.3, 6.2, 6.4
- `docs/design/evidence.md`, E12, E15
