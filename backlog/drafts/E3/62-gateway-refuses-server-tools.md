+++
id = "E3-62"
title = "Gateway: a Messages request whose tools hold one of Anthropic's own tools is refused with request_refused, so that Claude Code's web search fails at once and the session goes on"
labels = ["size:M", "kind:app"]
epic = 5
+++

## Context

In spike S3's second-session run on the coursework pool (the 8-bit build, with issue #795's
`tool_reference_as_text` deployed), the steps `open`, `skill`, `echo`, `pdf` and `agents` passed.
In `search` ("Search the web with your web search tool", part 2 of
`spikes/s3_second_session/TASK.md`), every refused request was a 502 `engine_error`: one request
and Claude Code's retries, then again on the model's next search. The engine's log holds, for each,
only its access line, `"POST /v1/messages HTTP/1.1" 400 Bad Request`: no `create_messages` error,
no traceback.

**The refused request is the web search's own request, not a turn of the session.** Claude Code's
`WebSearch` is a tool of its main loop like any other, with an `input_schema`, and those requests
are accepted. To run a search, it sends a Messages request of its own: a fresh one-message
conversation whose only tool is a tool Anthropic's service runs. Recorded from the pinned Claude
Code, 2.1.323, run against a fake Messages server on a loopback port (no endpoint, no key), its
body is, its system text and `metadata` left out:

```json
{"model": "<the session's model>", "max_tokens": 128000, "stream": true,
 "messages": [{"role": "user", "content": [{"type": "text", "text": "Perform a web search for the query: <query>"}]}],
 "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
 "tool_choice": {"type": "auto"}, "output_config": {"effort": "medium"}}
```

- **The gateway forwards that tool.** `_TOOL` in `services/gateway/rhi_gateway/formats/messages.py`
  describes every entry of `tools`, so this one keeps its `type` and `name`, loses `max_uses`, and
  reaches the engine as `{"type": "web_search_20250305", "name": "web_search"}`, with no
  `input_schema`. The module's docstring says such an entry is dropped ("the tools Anthropic
  defines or runs (every kind of `tools` entry but `Tool`)"); it is not. The recorded body, through
  the Messages filter as `rhi_gateway.main.build` wires it with the coursework pool of
  `config.example.toml`, comes out with exactly that entry.
- **vLLM 0.33.0 refuses it before it runs anything.** `AnthropicTool` in
  `vllm/entrypoints/anthropic/protocol.py` (line 138 at the tag) requires `input_schema`, so
  FastAPI's validation of the request body fails, `tools.0.input_schema: Field required` (shown with
  the tag's unmodified `protocol.py` and pydantic 2.12), before `create_messages` is called. Its
  handler, `validation_exception_handler` in
  `vllm/entrypoints/serve/exception_handling/handlers/validation.py` (line 149), logs only under
  `--log-error-stack` and answers 400: hence the lone access line. This is vLLM issue 46790 (open),
  the same error from Claude Code's web search. Pull request 64575, merged after v0.33.0, makes the
  400 name the tool's type; it is still a 400.
- **The gateway makes it a 502, and Claude Code retries a 502.** An engine 400 that is not a
  context-length refusal is `engine_error` (`_cause_of_refusal` in
  `services/gateway/rhi_gateway/proxy.py`, issue #793). Against the fake answering 502, Claude Code
  2.1.323 sent the search's request 11 times over about 176 seconds, then gave the model the
  search's failure, and the model may search again.

**The fix.** No engine behind this gateway runs Anthropic's tools, so the request cannot be made
acceptable in its place. Dropping the entry would be worse than the refusal: the model would answer
"Perform a web search" from memory, and Claude Code hands such an answer to the session as
"Web search results for query: ..." with no error (measured against the fake), so the session would
cite addresses no search returned. vLLM's maintainers chose the same for that reason (tracking issue
64810; pull request 46797, which dropped the tool, was closed). So the gateway refuses the request
itself, 400 `request_refused`, before it is forwarded: for every pool, as the blocks of those tools
are replaced for every pool (`SERVER_TOOL_BLOCKS`), and on both Messages routes, since token
counting takes the same `tools`.

This keeps ADR 0003, decision 7: that decision replaces blocks because a block stays in the
history and a refusal would end the session. A `tools` entry of the request is in no history, and
Claude Code's search sends it in a request of its own. Against the fake answering the gateway's own
`request_refused` body, Claude Code 2.1.323 sent the search's request twice (the second without one
beta header), then the session's next request carried a tool result marked as an error, "API Error:
400 The body of the request was refused.", and the turn went on, all within a second. The student
guide already says Claude Code's built-in web search does not work here (section 7). The rule reads
the top-level `tools` only: a tool definition inside a system message's tool addition is history,
and this issue leaves it as it is.

The entry is refused whatever its `type` names when that is not `custom`: the tools Anthropic's
service runs (web search, web fetch, code execution) and the tools Anthropic defines for the client
(`bash_*`, `text_editor_*`), which carry no `input_schema` either and which vLLM 0.33.0 refuses the
same way. A `Tool` written with `"type": "custom"`, or with no `type`, is forwarded as now.

There is no capture: a refusal has no engine answer to replay, and an exchange only the gateway took
part in is never replayed (`tests/captures/README.md`). The unit test holds the request in the
recorded shape instead.

Suggested branch: `fix/gateway-refuses-server-tools`

## Deliverables

- [ ] `services/gateway/rhi_gateway/formats/messages.py` refuses, with the cause request_refused, every Messages request whose top-level tools field holds an entry with a type that is present, not null and not custom, for every pool, before anything is forwarded to an engine.
- [ ] The refusal applies to both Messages routes as the gateway's entry point builds their filters, the token count included, and a tool definition inside a system message's tool addition is never refused by it.
- [ ] The docstring of `services/gateway/rhi_gateway/formats/messages.py` says that such an entry refuses the request, that the blocks of those tools in the history are still replaced, and why the request is refused and not rewritten: no engine here runs those tools, the engine refuses the entry, and a request without it would be answered as if a search had run.
- [ ] `tests/unit/gateway/test_server_tools.py` sends Claude Code 2.1.323's web-search request, in the shape of the Context, through the Messages and the token-count filters, and one request each with a tools entry of type bash_20250124, of type custom, and with no type.
- [ ] `docs/design/system-design.md` says in section 6.2, step 4, that a request declaring one of Anthropic's own tools is refused and why, and its step 8 table names that case in the row of request_refused.
- [ ] `docs/design/evidence.md` replaces the row of section E28 that says, from memory, that Claude Code's built-in web search would not work behind this endpoint by what is now known: the request Claude Code 2.1.323 sends for a search, and the 400 vLLM 0.33.0 answers to it, with vLLM issue 46790 and pull request 64575 as sources.
- [ ] `docs/student-guide.md` names, in the row of request_refused, a request that asks for a tool Anthropic's service runs, such as Claude Code's built-in web search.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_server_tools.py` passes: the web-search request and the bash_20250124 request are refused with status 400 and the cause request_refused on both Messages routes, and the requests whose tool has type custom or no type are forwarded with their tools unchanged.
- [ ] `uv run --locked pytest tests/unit/gateway/test_body_messages.py tests/unit/gateway/test_blocks_as_text.py tests/unit/gateway/test_coursework_profile.py tests/replay/test_replay.py` passes.
- [ ] `scripts/check.sh fast` exits 0.
- [ ] A rerun of spike S3's second-session job on the coursework pool writes `step` lines for `echo`, `pdf`, `agents` and `search` with no refused request whose cause is `engine_error`, and the `search` line has `search_called` true and every refused request of that step has the cause `request_refused` (needs gpu:capacity).

## References

- `docs/design/system-design.md`, sections 6.1 and 6.2, steps 4 and 8
- `docs/adr/0003-sessions-only-and-usage-logged.md`, decision 7
- `services/gateway/rhi_gateway/formats/messages.py`, `_TOOL`, `SERVER_TOOL_BLOCKS` and `blocks_as_text`
- `services/gateway/rhi_gateway/body.py`, steps 4 and 5
- `services/gateway/rhi_gateway/proxy.py`, `_cause_of_refusal`
- `spikes/s3_second_session.py`, the step `search` and `search_of`
- `tests/captures/README.md`, the captures of the real harnesses
- vLLM at the tag v0.33.0, its Anthropic request model and its validation handler (the two files named in the Context); issues 46790 and 64810, pull requests 64575 and 46797, at https://github.com/vllm-project/vllm
