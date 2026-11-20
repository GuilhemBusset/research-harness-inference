"""The request profile's entry for reasoning effort (`rhi_gateway.profile.reasoning_effort`).

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
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.profile import reasoning_effort

# The coursework pool's mapping (system design, section 17).
SECTION_17 = {"low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh", "max": "xhigh"}
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
}
ASK = [{"role": "user", "content": "Hello"}]


def with_effort(route: routes.Route, effort: str | None) -> dict[str, Any]:
    """A request of the route's format, with the effort in that format's own field, or none."""
    if route is routes.MESSAGES:
        return {"model": "pool", "max_tokens": 64, "messages": ASK} | (
            {} if effort is None else {"output_config": {"effort": effort}}
        )
    if route is routes.CHAT_COMPLETIONS:
        return {"model": "pool", "messages": ASK} | ({} if effort is None else {"reasoning_effort": effort})
    return {"model": "pool", "input": "Hello"} | ({} if effort is None else {"reasoning": {"effort": effort}})


def effort_of(route: routes.Route, body: dict[str, Any]) -> str | None:
    if route is routes.MESSAGES:
        return body.get("output_config", {}).get("effort")
    if route is routes.CHAT_COMPLETIONS:
        return body.get("reasoning_effort")
    return body.get("reasoning", {}).get("effort")


def sent(route: routes.Route, body: dict[str, Any], mapping: dict[str, str] | None = SECTION_17) -> dict[str, Any]:
    """What the engine receives for a body on a route, for a pool whose profile is this entry."""
    entry = {**MOCK, "request_profile": ["reasoning_effort"]}
    (pool,) = parse_pools({"pool": entry if mapping is None else {**entry, "reasoning_effort": mapping}})
    rewrites = profile.rewrites(profile.build([pool]), route)
    body_filter = {
        routes.MESSAGES: BodyFilter(messages.MESSAGES, (pool,), profiles=rewrites),
        routes.CHAT_COMPLETIONS: ChatBodyFilter((pool,), profiles=rewrites),
        routes.RESPONSES: BodyFilter(responses.RESPONSES, (pool,), profiles=rewrites),
    }[route]
    raw = json.dumps(body).encode()
    return json.loads(body_filter.apply(raw, student="student-1", request_id="gateway-id").body)


GENERATION = pytest.mark.parametrize(
    "route", [routes.MESSAGES, routes.CHAT_COMPLETIONS, routes.RESPONSES], ids=lambda route: route.wire_format
)


@pytest.mark.parametrize(
    ("asked", "received"),
    [("low", "low"), ("medium", "medium"), ("high", "xhigh"), ("xhigh", "xhigh"), ("max", "xhigh")],
)
@GENERATION
def test_the_effort_reaches_the_engine_as_the_pool_maps_it(route: routes.Route, asked: str, received: str) -> None:
    assert effort_of(route, sent(route, with_effort(route, asked))) == received


@GENERATION
def test_a_request_without_an_effort_reaches_the_engine_without_one(route: routes.Route) -> None:
    body = sent(route, with_effort(route, None))
    assert effort_of(route, body) is None
    assert "output_config" not in body and "reasoning_effort" not in body and "reasoning" not in body


@GENERATION
def test_an_effort_the_mapping_does_not_name_is_left_as_sent(route: routes.Route) -> None:
    assert effort_of(route, sent(route, with_effort(route, "minimal"))) == "minimal"
    # A pool that names the entry and maps nothing leaves every effort as sent.
    assert effort_of(route, sent(route, with_effort(route, "high"), mapping=None)) == "high"


def test_the_entry_names_its_defect_and_the_three_generation_routes() -> None:
    assert reasoning_effort.ENTRY.defect == "https://github.com/vllm-project/vllm/issues/58647"
    assert reasoning_effort.ENTRY.routes == {routes.MESSAGES, routes.CHAT_COMPLETIONS, routes.RESPONSES}
