"""The request profile's entry for reasoning effort (system design, sections 6.1 and 6.2, step 4).

Claude Code asks for reasoning effort `high` by default. The coursework model's chat template
accepts `xhigh`, `medium` and `low` and raises an error on anything else, so every default request
would be refused (evidence E28). This entry replaces the effort a request sets by the value the
pool's mapping gives for it (`rhi_common.pool_config`, `reasoning_effort`). The mapping is
configuration because it is also a lever on speed (section 4.7); section 17 gives the coursework
pool's: `low` and `medium` pass, and `high`, `xhigh` and `max` become `xhigh`.

vLLM has a flag for this mapping; the gateway does it instead, so that a unit test proves it
without a GPU. The template is the same whatever the route, so the entry applies to the three
generation routes, each in the field its format's allow-list keeps for the effort. A request that
sets no effort, a value the mapping does not name, and a pool with no mapping are left as sent.
"""

from __future__ import annotations

from typing import Any

from rhi_common.pool_config import Pool
from rhi_gateway import routes
from rhi_gateway.errors import WireFormat
from rhi_gateway.profile import Entry
from rhi_gateway.routes import Route

# Where each format keeps its reasoning effort: the object that holds it, then its key in that object.
# A Chat Completions request holds it at the top level.
_FIELD: dict[WireFormat, tuple[str | None, str]] = {
    WireFormat.MESSAGES: ("output_config", "effort"),
    WireFormat.CHAT_COMPLETIONS: (None, "reasoning_effort"),
    WireFormat.RESPONSES: ("reasoning", "effort"),
}


def rewrite(body: dict[str, Any], pool: Pool, route: Route) -> dict[str, Any]:
    """The body with its reasoning effort mapped by the pool's table, in its format's own field."""
    mapping = pool.reasoning_effort
    if not mapping or route.wire_format is None:
        return body
    parent, key = _FIELD[route.wire_format]
    holder = body if parent is None else body.get(parent)
    if not isinstance(holder, dict):
        return body
    effort = holder.get(key)
    if not isinstance(effort, str) or effort not in mapping:
        return body
    mapped = {**holder, key: mapping[effort]}
    return mapped if parent is None else {**body, parent: mapped}


ENTRY = Entry(
    name="reasoning_effort",
    wire_formats=frozenset(_FIELD),
    routes=frozenset({routes.MESSAGES, routes.CHAT_COMPLETIONS, routes.RESPONSES}),
    defect="https://github.com/vllm-project/vllm/issues/58647",
    rewrite=rewrite,
)
