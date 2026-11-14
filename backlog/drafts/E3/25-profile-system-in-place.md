+++
id = "E3-25"
title = "Request profile entry: send a system message placed mid-conversation as user content in its place"
labels = ["size:S", "kind:app"]
epic = "E3"
blocked_by = ["E3-23", "E3-43"]
+++

## Context

Claude Code places short system messages in the middle of a conversation and changes them from turn
to turn. The coursework model's template wants system text first, so vLLM moves them all to the top
of the prompt. The top then differs on every turn, nothing after it can be served from the cache,
and each turn recomputes the whole conversation (section 6.1; evidence E28). The fix is an open pull
request, in no release (https://github.com/vllm-project/vllm/pull/53501, for
https://github.com/vllm-project/vllm/issues/63331).

The profile's second entry sends such a message as user content in the same place, so that the
engine does not move it and the cache survives (section 6.2, step 4). One reporter replayed a real
Claude Code session on this model with exactly that rewrite: the first token came after 1.9 to 5.9
seconds a turn instead of 30 to 43 (evidence E29). When the profile fails, the cached share of input
falls to about a third (section 7.3).

Skills loaded and tools added in mid-session arrive in those messages as tool-addition and
tool-removal blocks, which vLLM 0.33 accepts; the entry changes the messages' role and keeps the
blocks (section 4.2). What that does to the skill or tool has not been measured, and spike S3 checks
it (issue E3-35).

A message counts as placed mid-conversation when it comes after the first message that is not a
system message: those before it are already at the top, where the engine puts them anyway. In
Messages the opening system prompt is a field of its own and stays as sent. The OpenAI formats also
call a system message a developer message, and both allow-lists keep either wherever it sits (issues
E2-18 and E2-19).

## Deliverables

- [ ] `services/gateway/rhi_gateway/profile/system_in_place.py` defines the entry system-in-place: a message with the system role, or in the OpenAI formats the developer role, that comes after the conversation's first message that is neither, becomes a message with the user role at the same position, with the same content.
- [ ] System and developer messages before the conversation's first other message, and the top-level system prompt of Messages, are left as sent; tool-addition and tool-removal blocks stay in their message, in their order.
- [ ] The entry names its defect, https://github.com/vllm-project/vllm/issues/63331, and applies to every forwarded route, token counting included.
- [ ] `tests/captures/profile-system-in-place/` holds a capture, built under the capture rules, of a Messages request shaped like Claude Code's with two system messages between turns, stating as its profile this entry alone, holding no student key, whose engine-side body carries them as user messages in place.
- [ ] `tests/unit/gateway/test_profile_system.py` covers the entry in each format.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_profile_system.py` passes: in each format, a system message between two user turns reaches the engine as a user message at the same index with the same content, the leading system message of a Chat Completions request is unchanged, and a tool-addition block stays in its message.
- [ ] `uv run --locked pytest tests/replay/test_replay.py` passes with the capture of `tests/captures/profile-system-in-place/`.

## References

- `docs/design/system-design.md`, sections 4.2, 6.1, 6.2, 7.3
- `docs/design/evidence.md`, E28, E29
