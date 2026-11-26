"""The request profile's entry for tool references (system design, sections 6.1 and 6.2, step 4).

Claude Code's tool search defers every MCP tool. When the model loads one with `ToolSearch`, the
tool's result holds `tool_reference` items that name the tools found. vLLM 0.33 passes such an
item on to the model's chat template, and the coursework model's template raises an error on an
item that is no text, image or video. Because the item stays in the history, every later request
of the session is refused too: the session ends (section 6.1). Two fixes are open upstream and
are in no release.

This entry replaces each tool reference inside a tool result by a text block, in its place, that
holds one line: `[tool reference: <name>]`. The line keeps the tool's name, so that the model reads
which tool the search found. The template lists every declared tool in full, deferred or not, so
the model can call the tool the line names. It is the text one of the upstream fixes writes
(https://github.com/vllm-project/vllm/pull/52498), so the model reads the same thing once the
pinned engine carries a fix and this entry is deleted.

The name is the item's `tool_name`, or its `name` when it has no `tool_name`, as upstream reads
it; an item that gives neither as text becomes `[tool reference]`. The name comes from the request
and goes back into the same conversation: it is no name of the deployment.

Only the content of a tool result is looked at, which is where the format puts the item. Every
other block and item keeps its place and its content, and a content written as one string is left
as sent.

The template refuses the item whatever the route, and token counting must count what the engine
will read, so the entry applies to both Messages routes.
"""

from __future__ import annotations

from typing import Any, Final

from rhi_common.pool_config import Pool
from rhi_gateway import routes
from rhi_gateway.errors import WireFormat
from rhi_gateway.profile import Entry
from rhi_gateway.routes import Route

# Where a tool reference gives its tool's name: the first of the two that holds one.
_NAME_FIELDS: Final = ("tool_name", "name")


def _as_text(item: dict[str, Any]) -> dict[str, str]:
    """The text block that replaces one tool reference: a line with the tool's name, or with none."""
    for field in _NAME_FIELDS:
        name = item.get(field)
        if isinstance(name, str) and name:
            return {"type": "text", "text": f"[tool reference: {name}]"}
    return {"type": "text", "text": "[tool reference]"}


def _items(content: list[object]) -> list[object]:
    """A tool result's content with each tool reference replaced by its line."""
    return [
        _as_text(item) if isinstance(item, dict) and item.get("type") == "tool_reference" else item for item in content
    ]


def _blocks(content: object) -> object:
    """A message's content with the content of each tool result rewritten, and every other block as it was."""
    if not isinstance(content, list):
        return content
    return [
        {**block, "content": _items(block["content"])}
        if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list)
        else block
        for block in content
    ]


def rewrite(body: dict[str, Any], pool: Pool, route: Route) -> dict[str, Any]:
    """The body with every tool reference inside a tool result replaced by its line of text in its place."""
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
    name="tool_reference_as_text",
    wire_formats=frozenset({WireFormat.MESSAGES}),
    routes=frozenset({routes.MESSAGES, routes.COUNT_TOKENS}),
    defect="https://github.com/vllm-project/vllm/issues/52489",
    rewrite=rewrite,
)
