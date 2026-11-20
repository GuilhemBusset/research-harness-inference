"""The request profile's entry for system messages placed mid-conversation (`rhi_gateway.profile.system_in_place`).

Each body goes through the body filter of its route with the profile the gateway's entry point
builds, so what a test reads is what the engine would receive.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rhi_common.pool_config import parse_pools
from rhi_gateway import profile, routes
from rhi_gateway.body import BodyFilter
from rhi_gateway.errors import WireFormat
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.profile import system_in_place

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
    "request_profile": ["system_in_place"],
}
REMINDER = [{"type": "text", "text": "The date has changed."}]
TOOL_BLOCKS = [
    {"type": "text", "text": "A skill was loaded."},
    {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "search"}},
    {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "lint"}},
]


def sent(route: routes.Route, body: dict[str, Any]) -> dict[str, Any]:
    """What the engine receives for a body on a route, for a pool whose profile is this entry."""
    (pool,) = parse_pools({"pool": MOCK})
    rewrites = profile.rewrites(profile.build([pool]), route)
    body_filter = {
        routes.MESSAGES: BodyFilter(messages.MESSAGES, (pool,), profiles=rewrites),
        routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, (pool,), profiles=rewrites),
        routes.CHAT_COMPLETIONS: ChatBodyFilter((pool,), profiles=rewrites),
        routes.RESPONSES: BodyFilter(responses.RESPONSES, (pool,), profiles=rewrites),
    }[route]
    raw = json.dumps(body).encode()
    return json.loads(body_filter.apply(raw, student="student-1", request_id="gateway-id").body)


def conversation(route: routes.Route, turns: list[dict[str, Any]]) -> dict[str, Any]:
    """A request of the route's format holding these turns as its conversation."""
    if route is routes.RESPONSES:
        return {"model": "pool", "input": [{"type": "message", **turn} for turn in turns]}
    if route.wire_format is WireFormat.MESSAGES:
        return {"model": "pool", "max_tokens": 64, "system": "Be brief.", "messages": turns}
    return {"model": "pool", "messages": turns}


def turns_of(route: routes.Route, body: dict[str, Any]) -> list[dict[str, Any]]:
    return body["input"] if route is routes.RESPONSES else body["messages"]


EVERY_FORMAT = pytest.mark.parametrize(
    "route",
    [routes.MESSAGES, routes.COUNT_TOKENS, routes.CHAT_COMPLETIONS, routes.RESPONSES],
    ids=lambda route: route.upstream_path,
)
OPENAI = pytest.mark.parametrize("route", [routes.CHAT_COMPLETIONS, routes.RESPONSES], ids=lambda r: r.upstream_path)


@EVERY_FORMAT
def test_a_system_message_between_two_user_turns_reaches_the_engine_as_a_user_message_in_its_place(
    route: routes.Route,
) -> None:
    text = [{"type": "input_text", "text": "The date has changed."}] if route is routes.RESPONSES else REMINDER
    asked = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": text},
        {"role": "user", "content": "Go"},
    ]
    received = turns_of(route, sent(route, conversation(route, asked)))
    assert [turn["role"] for turn in received] == ["user", "user", "user"]
    assert received[1]["content"] == text
    if route.wire_format is WireFormat.MESSAGES:
        assert sent(route, conversation(route, asked))["system"] == "Be brief."


@OPENAI
def test_a_developer_message_after_the_first_other_message_becomes_a_user_message(route: routes.Route) -> None:
    asked = [
        {"role": "developer", "content": "Opening rules."},
        {"role": "system", "content": "Opening system."},
        {"role": "user", "content": "Hello"},
        {"role": "developer", "content": "Later rules."},
        {"role": "assistant", "content": "Hi"},
        {"role": "system", "content": "Later system."},
    ]
    received = turns_of(route, sent(route, conversation(route, asked)))
    assert [turn["role"] for turn in received] == ["developer", "system", "user", "user", "assistant", "user"]
    assert [turn["content"] for turn in received] == [turn["content"] for turn in asked]


def test_the_leading_system_message_of_a_chat_completions_request_is_unchanged() -> None:
    leading = {"role": "system", "content": "You are a coding assistant."}
    received = sent(
        routes.CHAT_COMPLETIONS, {"model": "pool", "messages": [leading, {"role": "user", "content": "Hi"}]}
    )
    assert received["messages"] == [leading, {"role": "user", "content": "Hi"}]


@pytest.mark.parametrize("route", [routes.MESSAGES, routes.COUNT_TOKENS], ids=lambda route: route.upstream_path)
def test_tool_addition_and_removal_blocks_stay_in_their_message_in_their_order(route: routes.Route) -> None:
    asked = [
        {"role": "user", "content": "Hello"},
        {"role": "system", "content": TOOL_BLOCKS},
        {"role": "user", "content": "Go"},
    ]
    assert turns_of(route, sent(route, conversation(route, asked)))[1] == {"role": "user", "content": TOOL_BLOCKS}


def test_the_entry_names_its_defect_and_every_forwarded_route() -> None:
    assert system_in_place.ENTRY.defect == "https://github.com/vllm-project/vllm/issues/63331"
    forwarded = {route for route in routes.TABLE if route.kind is routes.Kind.FORWARDED}
    assert system_in_place.ENTRY.routes == forwarded
