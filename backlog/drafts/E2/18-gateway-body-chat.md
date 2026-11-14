+++
id = "E2-18"
title = "Gateway: the body allow-list for OpenAI Chat Completions"
labels = ["size:S", "kind:app"]
epic = "E2"
blocked_by = ["E2-16"]
+++

## Context

OpenAI SDK scripts, aider and opencode speak Chat Completions (section 6.1). On this format the
engine reports usage only in an optional final chunk, so the gateway asks for that chunk on every
stream and remembers whether the client asked for it too (section 6.2, step 6).

The request profile of phase 2 runs after this filter and maps reasoning effort to a value the
model's template accepts (section 6.2, step 4). So the list keeps the format's reasoning-effort
field, `reasoning_effort` in OpenAI's reference, whatever its value, and it keeps system and
developer messages wherever they sit in the conversation.

## Deliverables

- [ ] `services/gateway/rhi_gateway/formats/chat.py` lists the standard fields of a Chat Completions request, at the top level and inside messages and tools, each with the place in OpenAI's reference it comes from.
- [ ] The list keeps the reasoning-effort field as sent, whatever its value, system and developer messages wherever they sit, and images with inline data.
- [ ] On a stream it always asks the engine for the final usage chunk, and records whether the client had asked for it.
- [ ] `tests/unit/gateway/test_body_chat.py` covers a request with tools and one without.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_body_chat.py` passes: standard fields pass, unknown fields are gone, and a stream request leaves with the usage option set whether or not the client set it.
- [ ] `tests/unit/gateway/test_body_chat.py::test_what_the_profile_rewrites_is_kept` finds reasoning effort high and a system message between two user messages forwarded unchanged and in place.

## References

- `docs/design/system-design.md`, sections 6.1, 6.2
