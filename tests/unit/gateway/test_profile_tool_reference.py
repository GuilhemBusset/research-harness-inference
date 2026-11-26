"""The request profile's entry for tool references (`rhi_gateway.profile.tool_reference_as_text`).

Each body goes through the Messages body filter of its route with the profile the gateway's entry
point builds, so what a test reads is what the engine would receive.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rhi_common.pool_config import parse_pools
from rhi_gateway import profile, routes
from rhi_gateway.body import BodyFilter
from rhi_gateway.formats import messages
from rhi_gateway.profile import tool_reference_as_text

MOCK = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "prices": {"t4g.small": 0.02},
    "wake": "on-demand",
    "max_instances": 1,
    "warm_instances": 0,
    "target_concurrency": 8,
    "context_length": 262144,
    "largest_output": 32000,
    "request_profile": ["tool_reference_as_text"],
}
FOUND = {"type": "text", "text": "Two tools were found."}
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}
SEARCH = {"type": "tool_use", "id": "toolu_1", "name": "ToolSearch", "input": {"query": "select:mcp__notes__read"}}
BOTH_ROUTES = pytest.mark.parametrize(
    "route", [routes.MESSAGES, routes.COUNT_TOKENS], ids=lambda route: route.upstream_path
)


def text(line: str) -> dict[str, str]:
    return {"type": "text", "text": line}


def turns(*items: dict[str, Any]) -> list[dict[str, Any]]:
    """A conversation whose one tool result holds `items`, among the turns the entry must leave as they are."""
    return [
        {"role": "user", "content": [{"type": "text", "text": "Read my notes."}]},
        {"role": "assistant", "content": [SEARCH]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": list(items)},
                {"type": "tool_result", "tool_use_id": "toolu_0", "content": "A result written as one string."},
                {"type": "text", "text": "Go on."},
            ],
        },
        {"role": "user", "content": "Plain text stays."},
    ]


def sent(route: routes.Route, body: dict[str, Any]) -> dict[str, Any]:
    """What the engine receives for a body on a route, for a pool whose profile is this entry."""
    (pool,) = parse_pools({"pool": MOCK})
    rewrites = profile.rewrites(profile.build([pool]), route)
    shape = {routes.MESSAGES: messages.MESSAGES, routes.COUNT_TOKENS: messages.COUNT_TOKENS}[route]
    raw = json.dumps(body).encode()
    return json.loads(BodyFilter(shape, (pool,), profiles=rewrites).apply(raw, student="s", request_id="g").body)


def types_in(value: object) -> set[str]:
    """Every block type anywhere in a value."""
    if isinstance(value, list):
        return set().union(*(types_in(item) for item in value))
    if isinstance(value, dict):
        own = {value["type"]} if isinstance(value.get("type"), str) else set()
        return own.union(*(types_in(item) for item in value.values()))
    return set()


@BOTH_ROUTES
def test_each_tool_reference_reaches_the_engine_as_its_line_in_its_place_and_every_other_block_unchanged(
    route: routes.Route,
) -> None:
    asked = turns(
        FOUND,
        {"type": "tool_reference", "tool_name": "mcp__notes__read"},
        IMAGE,
        {"type": "tool_reference", "tool_name": "mcp__notes__write"},
    )
    tools = [{"name": "mcp__notes__read", "input_schema": {"type": "object"}, "defer_loading": True}]
    body = {"model": "pool", "max_tokens": 64, "system": "Be brief.", "tools": tools, "messages": asked}

    received = sent(route, body)

    expected = turns(
        FOUND, text("[tool reference: mcp__notes__read]"), IMAGE, text("[tool reference: mcp__notes__write]")
    )
    assert received["messages"] == expected
    assert (received["system"], received["tools"]) == ("Be brief.", tools)
    assert "tool_reference" not in types_in(received)


@pytest.mark.parametrize(
    ("reference", "line"),
    [
        ({"tool_name": "mcp__notes__read"}, "[tool reference: mcp__notes__read]"),
        ({"name": "mcp__notes__read"}, "[tool reference: mcp__notes__read]"),
        ({"tool_name": "mcp__notes__read", "name": "another"}, "[tool reference: mcp__notes__read]"),
        ({"tool_name": "", "name": "mcp__notes__read"}, "[tool reference: mcp__notes__read]"),
        ({}, "[tool reference]"),
        ({"tool_name": None, "name": 7}, "[tool reference]"),
    ],
    ids=["tool_name", "name", "both", "empty-tool_name", "neither", "no-text"],
)
@BOTH_ROUTES
def test_the_line_names_the_tool_by_its_tool_name_or_else_its_name_or_else_names_none(
    route: routes.Route, reference: dict[str, Any], line: str
) -> None:
    received = sent(route, {"model": "pool", "messages": turns(FOUND, {"type": "tool_reference", **reference})})
    assert received["messages"] == turns(FOUND, text(line))


@BOTH_ROUTES
def test_a_tool_reference_that_is_no_item_of_a_tool_result_is_left_as_sent(route: routes.Route) -> None:
    # A system message that adds a tool names it with a tool reference, which the engine takes.
    addition = {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "mcp__notes__read"}}
    asked = [*turns(FOUND), {"role": "system", "content": [addition]}]
    assert sent(route, {"model": "pool", "messages": asked})["messages"] == asked


def test_the_entry_names_its_defect_and_both_messages_routes() -> None:
    assert tool_reference_as_text.ENTRY.defect == "https://github.com/vllm-project/vllm/issues/52489"
    assert tool_reference_as_text.ENTRY.routes == {routes.MESSAGES, routes.COUNT_TOKENS}
