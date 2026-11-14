+++
id = "E3-26"
title = "Request profile entry: replace a document block by a line saying the endpoint cannot read it"
labels = ["size:S", "kind:app"]
epic = "E3"
blocked_by = ["E3-23", "E3-43"]
+++

## Context

Claude Code sends a PDF as a document block. vLLM refuses it, and because the block stays in the
history, every later request of that session is refused too (section 6.1). vLLM's replay of Claude
Code traffic lists document blocks, which is how a PDF read by the Read tool arrives, among the blocks
that fail; a fix was merged for the tool-addition and tool-removal blocks, and none was found for
document blocks (evidence E28, from https://github.com/vllm-project/vllm/issues/58647, item 5).

The profile's third entry replaces a document block by a line of text saying that the endpoint
cannot read it, because the engine's refusal would otherwise end the session (section 6.2, step 4).
The student guide says to convert a PDF to text first, which the agent can do itself (section 4.2).
A PDF read by a tool arrives inside the tool's result, so the entry looks there too. The body filter
keeps document blocks with inline data in both places (issue E2-17).

## Deliverables

- [ ] `services/gateway/rhi_gateway/profile/document_as_text.py` defines the entry document-as-text: in a Messages request, every document block, in a message's content or inside a tool result's content, is replaced in its place by a text block holding one fixed sentence, defined once in the module, which says that the endpoint cannot read documents.
- [ ] Every other block keeps its place and its content.
- [ ] The entry names its defect, https://github.com/vllm-project/vllm/issues/58647, and applies to both Messages routes, token counting included.
- [ ] `tests/captures/profile-document-as-text/` holds a capture, built under the capture rules, of a Messages request shaped like Claude Code's after a PDF was read, stating as its profile this entry alone, holding no student key, whose engine-side body carries the sentence where the document was.
- [ ] `tests/unit/gateway/test_profile_document.py` covers the entry.

## Acceptance criteria

- [ ] `uv run --locked pytest tests/unit/gateway/test_profile_document.py` passes: a document block in a user message and another inside a tool result each reach the engine as the fixed sentence in their place, an image block beside them reaches it unchanged, and the forwarded body holds no document block.
- [ ] `uv run --locked pytest tests/replay/test_replay.py` passes with the capture of `tests/captures/profile-document-as-text/`.

## References

- `docs/design/system-design.md`, sections 4.2, 6.1, 6.2
- `docs/design/evidence.md`, E28
