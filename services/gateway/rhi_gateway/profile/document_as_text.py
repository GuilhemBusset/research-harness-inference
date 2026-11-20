"""The request profile's entry for document blocks (system design, sections 6.1 and 6.2, step 4).

Claude Code sends a PDF as a document block. vLLM refuses the block, and because it stays in the
history, every later request of the session is refused too: the session ends (section 6.1). A fix
was merged upstream for the blocks that add and remove a tool, and none was found for document
blocks (evidence E28). This entry replaces each document block by a text block, in its place, that
holds one fixed sentence saying that the endpoint cannot read documents, so the session goes on and
the model can tell the student so. The student guide says to convert a PDF to text first, which
the agent can do itself (section 4.2).

A PDF that a tool read arrives inside the tool's result, so the entry looks in two places: the
content of each message, and the content of each tool result in it. Every other block keeps its
place and its content, and a content written as one string is left as sent.

The engine refuses the block whatever the route, and token counting must count what the engine
will read, so the entry applies to both Messages routes.
"""

from __future__ import annotations

from typing import Any, Final

from rhi_common.pool_config import Pool
from rhi_gateway import routes
from rhi_gateway.errors import WireFormat
from rhi_gateway.profile import Entry
from rhi_gateway.routes import Route

# What the engine reads where a document was.
SENTENCE: Final = "A document was attached here, but this endpoint cannot read documents: convert it to text first."

_TEXT: Final = {"type": "text", "text": SENTENCE}


def _is_document(block: object) -> bool:
    return isinstance(block, dict) and block.get("type") == "document"


def _blocks(content: object) -> object:
    """A content list with each document block replaced by the sentence, and each tool result's own content too."""
    if not isinstance(content, list):
        return content
    replaced: list[object] = []
    for block in content:
        if _is_document(block):
            replaced.append(dict(_TEXT))
        elif isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            replaced.append({**block, "content": _blocks(block["content"])})
        else:
            replaced.append(block)
    return replaced


def rewrite(body: dict[str, Any], pool: Pool, route: Route) -> dict[str, Any]:
    """The body with every document block, in a message or in a tool result, replaced by the sentence in its place."""
    conversation = body.get("messages")
    if not isinstance(conversation, list):
        return body
    replaced = [
        {**message, "content": _blocks(message["content"])}
        if isinstance(message, dict) and "content" in message
        else message
        for message in conversation
    ]
    return {**body, "messages": replaced}


ENTRY = Entry(
    name="document_as_text",
    wire_formats=frozenset({WireFormat.MESSAGES}),
    routes=frozenset({routes.MESSAGES, routes.COUNT_TOKENS}),
    defect="https://github.com/vllm-project/vllm/issues/58647",
    rewrite=rewrite,
)
