+++
id = "E3-27"
title = "The coursework pool's request profile: three entries and the effort mapping of section 17"
labels = ["size:S", "kind:app"]
epic = "E3"
blocked_by = ["E2-76", "E3-10", "E3-24", "E3-25", "E3-26"]
+++

## Context

For the coursework model the request profile has three entries (section 6.2, step 4): reasoning
effort mapped to a value the model's template accepts (issue E3-24), a system message placed
mid-conversation sent as user content in its place (issue E3-25), and a document block replaced by a
line of text (issue E3-26). Section 17 gives the effort mapping's default for this pool: `low` and
`medium` unchanged; `high`, `xhigh` and `max` as `xhigh`.

This issue gives the example configuration's coursework pool that profile, and shows one request
carrying all three defects reaching the engine in the form the model accepts. The upgrade pool of
phase 4 shares the model's template and parsers, and so the same profile (section 6.1). Whether the
real harnesses work with it on a GPU is spike S3 (issues E3-41 and E3-35).

The gateway builds every configured pool's profile when it starts and refuses to start on a profile
it cannot build (issue E3-23), so the example configuration's profile is also shown by starting the
gateway's application with it, through the entry point and settings of issue E2-76, with stand-ins
for the stores it reads. A test that needs a student key generates it at run time (issue E0-16).

## Deliverables

- [ ] `config.example.toml` gives the coursework pool the request profile of section 6.2, the entries reasoning-effort, system-in-place and document-as-text, with the effort mapping of section 17: low to low, medium to medium, and high, xhigh and max to xhigh.
- [ ] `tests/unit/gateway/test_coursework_profile.py` sends a Messages request shaped like Claude Code's for the coursework pool of `config.example.toml` and checks the body the engine receives, and starts the gateway's application with the pools of `config.example.toml`.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_coursework_profile.py` passes: a request with effort high, a system message between two user turns and a document block inside a tool result reaches the engine with effort xhigh, that message as user content in its place, and the block replaced by the profile's sentence.
- [ ] `uv run --locked pytest tests/unit/common/test_pool_config.py tests/unit/test_config.py` passes with the pools of `config.example.toml`.
- [ ] `tests/unit/gateway/test_coursework_profile.py::test_the_gateway_starts` builds the gateway's application from the pools of `config.example.toml` through its entry point's settings and finds it started, with the coursework pool's profile built.
- [ ] The owner adds the coursework pool's request profile and effort mapping, as `config.example.toml` gives them, to the coursework pool of the configuration in SSM (needs aws:account).

## References

- `docs/design/system-design.md`, sections 6.1, 6.2, 17
