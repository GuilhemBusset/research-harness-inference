+++
id = "E3-58"
title = "Request profile: a tool reference inside a tool result reaches the coursework model as a line of text, so that Claude Code's tool search no longer ends the session"
labels = ["size:M", "kind:app"]
epic = 5
+++

## Context

In spike S3's second-session run on the coursework pool (the 4-bit build), the steps `open` and
`skill` passed. From `echo` on, every step got 11 refused requests, all 502 `engine_error`, which
is one request and Claude Code's 10 retries. The engine logged
`Error in create_messages: Unexpected item type in content.`, and no other `create_messages` error.

The refused item is a `tool_reference` inside a `tool_result`:

- **Claude Code sends it.** Claude Code 2.1.304 and later turns tool search on by default behind a
  custom `ANTHROPIC_BASE_URL` (code.claude.com/docs/en/mcp, "Tool search through an LLM gateway").
  Every MCP tool is then deferred (`defer_loading`). When the model loads one with Claude Code's
  `ToolSearch`, the tool's result holds `tool_reference` items that name the tools found. The
  `echo` step is the first to add an MCP server, and so the first with a deferred tool.
- **vLLM 0.33.0 passes it on.** `_convert_user_tool_result`, in
  `vllm/entrypoints/anthropic/serving.py`, sends such an item on as a `tool` message whose content
  is `[{"type": "tool_reference", "name": ...}]`. `vllm/renderers/chat_utils/chat_message.py` keeps
  that item as it is "for the chat template to expand".
- **The model's template refuses it.** In the coursework model's `chat_template.jinja` (the same in
  `Qwen/Qwen3.8-27B` and `Qwen/Qwen3.8-27B-FP8`), `render_content` knows image, video and text
  items. Any other item reaches `raise_exception('Unexpected item type in content.')`.
  `create_error_response` makes that template error a 400.
- **Upstream knows it.** This is vLLM issue 52489, reproduced there with this model's template.
  Two fixes are open and are in no release: pull request 52498 turns the item into a text block
  `[tool reference: <name>]`, and 67963 turns the error into a 400 that names tool search.

The timing fits. In `echo` the first refused request went out about two seconds after the step
began, after one answered request, the model's `ToolSearch` call. In `pdf`, `agents` and `search`
it was each turn's first request, half a second in. Each of those requests carried the earlier
tool result, which stays in the history.

**What the gateway does with it today.** The Messages allow-list keeps "the tool reference inside
a tool result" (`services/gateway/rhi_gateway/formats/messages.py`). Neither the shared
replacements (`blocks_as_text`) nor the coursework pool's request profile touches it, so it reaches
the engine as sent.

**The fix.** It follows ADR 0003, decision 7, and issue #175's `document_as_text`: a block the
engine refuses is replaced in its place by a line of text, so that the session goes on. The
refusal comes from this model's template, not from vLLM for every model, so the replacement is a
request profile entry, `tool_reference_as_text`, for the pools whose model has this template.

The line keeps the tool's name, so that the model reads which tool the search found. It is exactly
the text upstream pull request 52498 writes, `[tool reference: <name>]`, so the model reads the
same thing once the pinned engine carries a fix and the entry is deleted. The name is the
`tool_name` the item gives, or its `name`. It comes from the request and goes back into the same
conversation: it is no name of the deployment.

The template already lists every declared tool in full, deferred or not (`tool | tojson` for each
entry of `tools`), so the model can call the tool the line names.

**E3-56 does not help here.** The engine's answer is a 400, but its message does not contain
"maximum context length". Under E3-56 it is still answered 502 `engine_error`, and Claude Code
still retries it ten times. Claude Code 2.1.305 and later retries without tool search after a 400
or 422 that names tool search. vLLM's message does not name it, and the gateway relays no engine
text, so passing the 400 through would not help either. The replacement is what lets the session
go on.

The deployed configuration names a pool's profile entries, and the owner writes it to SSM. So the
coursework pool takes the entry in `config.example.toml` here, and in the deployed configuration as
an obligation of the owner.

Suggested branch: `fix/profile-tool-reference-as-text`

## Deliverables

- [ ] `services/gateway/rhi_gateway/profile/tool_reference_as_text.py` defines the entry `tool_reference_as_text`: in a Messages request, every item of type `tool_reference` inside a tool result's content is replaced in its place by a text block `[tool reference: <name>]`, the name being the item's `tool_name`, or its `name` when it has no `tool_name`.
- [ ] A `tool_reference` item that carries no name is replaced by a text block `[tool reference]`, and every other block and item keeps its place and its content.
- [ ] The entry names its defect, https://github.com/vllm-project/vllm/issues/52489, and applies to both Messages routes, token counting included.
- [ ] `services/gateway/rhi_gateway/profile/__init__.py` lists the fourth entry in its docstring.
- [ ] `config.example.toml` names `tool_reference_as_text` in the coursework pool's `request_profile`, after `document_as_text`.
- [ ] `tests/captures/profile-tool-reference-as-text/` holds a capture, built under the capture rules, of a Messages request shaped like Claude Code's after a `ToolSearch` call returned two tool references, stating as its profile this entry alone, holding no student key, whose engine-side body carries the two text blocks where the references were.
- [ ] `tests/unit/gateway/test_profile_tool_reference.py` covers the entry.
- [ ] `tests/unit/gateway/test_coursework_profile.py` expects the coursework pool's four entries, in order, and a Claude Code request holding a tool reference to reach the engine with the text block in its place.
- [ ] `docs/design/system-design.md` names the fourth defect among those of section 6.1 that decide whether a Claude Code session works, and the fourth entry in section 6.2, step 4.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_profile_tool_reference.py` passes. A tool reference inside a tool result reaches the engine as its text block in its place, a text item beside it reaches it unchanged, and the forwarded body holds no item of type `tool_reference`.
- [ ] `uv run --locked pytest tests/replay/test_replay.py tests/unit/gateway/test_coursework_profile.py tests/unit/gateway/test_profile.py tests/unit/test_config.py` passes with the capture of `tests/captures/profile-tool-reference-as-text/`.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] The deployed configuration of the coursework pool names `tool_reference_as_text` in its `request_profile`, after `document_as_text`, written to SSM with the owner's configuration tool (needs aws:account).
- [ ] A rerun of spike S3's second-session job on the coursework pool writes `step` lines for `echo`, `pdf`, `agents` and `search` with no refused request whose cause is `engine_error` (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 4.2, 6.1 and 6.2, step 4
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 7
- `services/gateway/rhi_gateway/profile/document_as_text.py`
- `services/gateway/rhi_gateway/formats/messages.py`
- `spikes/s3_second_session.py`, the steps
- `tests/captures/README.md`
