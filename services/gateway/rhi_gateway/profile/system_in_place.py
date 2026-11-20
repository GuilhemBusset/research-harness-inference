"""The request profile's entry for system messages placed mid-conversation (system design, sections 6.1 and 6.2, step 4).

Claude Code places short system messages in the middle of a conversation and changes them from turn
to turn. The coursework model's chat template wants system text first, so vLLM moves every system
message to the top of the prompt: the top then differs on every turn, nothing after it is served
from the cache, and each turn recomputes the whole conversation (evidence E28). This entry sends
such a message as user content in the same place, so that the engine does not move it and the
cache survives (evidence E29).

A message counts as placed mid-conversation when it comes after the conversation's first message
that is not a system message: those before it are already at the top, where the engine puts them
anyway, and are left as sent. The OpenAI formats also call a system message a developer message,
and the entry treats the two alike there. Only the role changes: the message keeps its index, its
content and its other fields, so the blocks that add and remove a tool stay in it, in their order
(section 4.2). The opening system prompt of Messages (`system`) and the instructions of Responses
(`instructions`) are fields of their own and are left as sent; so is a Responses `input` written as
one string.

The template is the same whatever the route, and token counting must count what the engine will
read, so the entry applies to every forwarded route.
"""

from __future__ import annotations

from typing import Any

from rhi_common.pool_config import Pool
from rhi_gateway import routes
from rhi_gateway.errors import WireFormat
from rhi_gateway.profile import Entry
from rhi_gateway.routes import Route

# Where each format keeps its conversation, and the roles that count as system there.
_CONVERSATION: dict[WireFormat, tuple[str, frozenset[str]]] = {
    WireFormat.MESSAGES: ("messages", frozenset({"system"})),
    WireFormat.CHAT_COMPLETIONS: ("messages", frozenset({"system", "developer"})),
    WireFormat.RESPONSES: ("input", frozenset({"system", "developer"})),
}


def _role(item: object) -> object:
    return item.get("role") if isinstance(item, dict) else None


def rewrite(body: dict[str, Any], pool: Pool, route: Route) -> dict[str, Any]:
    """The body with each system message after the conversation's first other message turned into a user message."""
    if route.wire_format is None:
        return body
    key, system = _CONVERSATION[route.wire_format]
    conversation = body.get(key)
    if not isinstance(conversation, list):
        return body
    first = next((index for index, item in enumerate(conversation) if _role(item) not in system), None)
    if first is None:
        return body
    moved = [
        {**item, "role": "user"} if index > first and _role(item) in system else item
        for index, item in enumerate(conversation)
    ]
    return {**body, key: moved}


ENTRY = Entry(
    name="system_in_place",
    wire_formats=frozenset(_CONVERSATION),
    routes=frozenset({routes.MESSAGES, routes.COUNT_TOKENS, routes.CHAT_COMPLETIONS, routes.RESPONSES}),
    defect="https://github.com/vllm-project/vllm/issues/63331",
    rewrite=rewrite,
)
