+++
id = "E3-47"
title = "Gateway: replace the Messages blocks this endpoint cannot serve by a line of text, never refuse them"
labels = ["size:M", "kind:app"]
epic = 5
+++

## Context

A block that the engine refuses stays in the conversation's history, so every later request of the
session carries it and is refused too: the session is lost. The request profile already replaces
a document block by a line of text for the coursework model (issue #175,
`services/gateway/rhi_gateway/profile/document_as_text.py`). Three more kinds of Messages block end
a session the same way (section 6.2, step 4; ADR 0003, decision 7):

- a **search result** block, which the allow-list keeps and the engine refuses;
- the blocks of the **tools Anthropic's own service runs**: server_tool_use, the result blocks of
  web search, web fetch and code execution, container_upload and browser_state. The allow-list
  drops their extra fields, the engine refuses each by its type, and one whose content is an
  object is refused by the filter itself (`services/gateway/rhi_gateway/formats/messages.py`);
- a **document given by URL**, which the filter refuses as a media reference that is not inline
  data (step 5 of `services/gateway/rhi_gateway/body.py`).

Each is replaced in its place by a text block holding one fixed sentence that says what was there
and that the endpoint cannot read it, so the session goes on and the model can tell the student.
Unlike the request profile, this holds for every pool: no engine behind this gateway runs
Anthropic's tools or fetches a URL, so the replacement is a rule of the Messages format, made before
the allow-list and the media rule apply. Every other media reference that is not inline data is
still refused, and an image given by URL still is.

Claude Code places such blocks in a message's content and inside a tool result's content, so both
places are looked at, as the document entry does. Both Messages routes apply it, token counting
included, so that a count counts what the engine will read.

## Deliverables

- [ ] `services/gateway/rhi_gateway/body.py` gains a step, after parsing and before the allow-list, that applies the replacement the caller gives for its wire format, and applies none when the caller gives none.
- [ ] `services/gateway/rhi_gateway/formats/messages.py` gives the replacement for both Messages routes: every search result block, every block of a tool Anthropic's service runs that the module lists, and every document block whose source is a URL, in a message's content or inside a tool result's content, is replaced in its place by a text block holding the fixed sentence of its kind.
- [ ] The three sentences are defined once in `services/gateway/rhi_gateway/formats/messages.py`, hold no number and no name of the deployment, and every other block keeps its place and its content.
- [ ] `tests/unit/gateway/test_blocks_as_text.py` covers the replacement through the Messages filter, on both routes.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_blocks_as_text.py` passes: a search result in a user message, a web search result inside a tool result and a document whose source is a URL each reach the forwarded body as their sentence in their place, a text block and an inline image beside them reach it unchanged, and none of the three requests is refused.
- [ ] `tests/unit/gateway/test_blocks_as_text.py::test_an_image_url_is_still_refused` finds a Messages request with an image given by URL refused with the cause request_refused.
- [ ] `uv run --locked pytest tests/unit/gateway/test_body_messages.py tests/unit/gateway/test_profile_document.py tests/replay/test_replay.py` passes.

## References

- `docs/design/system-design.md`, sections 6.1 and 6.2, step 4
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 7
- `services/gateway/rhi_gateway/profile/document_as_text.py`
