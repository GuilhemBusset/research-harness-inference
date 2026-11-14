+++
id = "E2-17"
title = "Gateway: the body allow-list for Anthropic Messages"
labels = ["size:M", "kind:app"]
epic = "E2"
blocked_by = ["E2-16"]
+++

## Context

Claude Code, in the terminal, headless and through the Agent SDK, and the Anthropic SDK speak
Anthropic Messages (section 6.1). The allow-list keeps the standard fields of the format and drops
the rest; token counting uses the same list without the generation fields.

Claude Code's requests are the ones this list must not break. vLLM's own replay of captured Claude
Code traffic records their shape (evidence E28, https://github.com/vllm-project/vllm/issues/58647):
about 25 tools with nested schemas, interleaved thinking with parallel tool calls, short system
messages placed in the middle of the conversation that change from turn to turn, and `max_tokens`
32000 on the main loop (evidence E15). Claude Code returns the model's earlier reasoning blocks with
each request, which the model must accept back (section 4.2), and reads screenshots and plots as
images inside tool results.

The request profile of phase 2 rewrites three things in these requests, after this filter (sections
6.1 and 6.2, step 4): the reasoning effort, which Claude Code sets to `high` by default; the system
messages placed mid-conversation; and document blocks, which is how Claude Code sends a PDF. So this
list keeps all three as they are sent, and refuses none of them. It also keeps the request's
`thinking` field, which vLLM 0.33 no longer drops (section 6.1), and the tool-addition and
tool-removal blocks Claude Code places in those system messages when a skill is loaded or a tool
added mid-session, which vLLM 0.33 accepts (section 4.2; evidence E28). The design does not name the
format's reasoning-effort field; the builder takes it from Anthropic's published reference and the
replay above, and cites where it comes from like every other entry.

## Deliverables

- [ ] `services/gateway/rhi_gateway/formats/messages.py` lists the standard fields of a Messages request, at the top level and inside messages, content blocks and tools, each with the place in Anthropic's reference it comes from.
- [ ] The list keeps the format's reasoning-effort field and the thinking field as sent, whatever their values, and the thinking and redacted-thinking blocks of assistant messages with their signatures.
- [ ] It keeps a message whose role is system wherever it sits in the conversation, with the tool-addition and tool-removal blocks it carries, in its place.
- [ ] It keeps document blocks with inline data, in a message's content and inside a tool result, and image blocks with inline data, in both places too.
- [ ] The same module gives the list for `POST /v1/messages/count_tokens`: the same fields without those that only generation uses.
- [ ] The output limit is clamped to the pool's, and a streaming flag is kept as sent; `tests/unit/gateway/test_body_messages.py` covers a request shaped like Claude Code's and one shaped like the SDK's.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_body_messages.py` passes: a request with tools, a system prompt, tool results, a thinking option and the thinking and redacted-thinking blocks of earlier turns with their signatures passes unchanged in those fields, and unknown top-level and nested fields are gone.
- [ ] `tests/unit/gateway/test_body_messages.py::test_what_the_profile_rewrites_is_kept` sends a request with reasoning effort high, a system message between two user turns, a document block in a user message and another in a tool result, and an image in a tool result, and finds each of them forwarded unchanged and in its place, with no refusal.
- [ ] `tests/unit/gateway/test_body_messages.py::test_the_output_limit_is_clamped` turns 32000 into the pool's limit when the pool's is lower.

## References

- `docs/design/system-design.md`, sections 4.2, 6.1, 6.2
- `docs/design/evidence.md`, E15, E28
