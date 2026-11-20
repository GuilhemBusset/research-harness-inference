"""The request profile's entry for document blocks (`rhi_gateway.profile.document_as_text`).

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
from rhi_gateway.profile import document_as_text

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
    "request_profile": ["document_as_text"],
}
SENTENCE = {"type": "text", "text": document_as_text.SENTENCE}
DOCUMENT = {
    "type": "document",
    "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQK"},
}
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}
ASKED = [
    {"role": "user", "content": [{"type": "text", "text": "Summarize this."}, DOCUMENT, IMAGE]},
    {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "notes.pdf"}}],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [DOCUMENT, IMAGE]},
            {"type": "text", "text": "Go on."},
        ],
    },
    {"role": "user", "content": "Plain text stays."},
]
EXPECTED = [
    {"role": "user", "content": [{"type": "text", "text": "Summarize this."}, SENTENCE, IMAGE]},
    ASKED[1],
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [SENTENCE, IMAGE]},
            {"type": "text", "text": "Go on."},
        ],
    },
    ASKED[3],
]
BOTH_ROUTES = pytest.mark.parametrize(
    "route", [routes.MESSAGES, routes.COUNT_TOKENS], ids=lambda route: route.upstream_path
)


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
def test_each_document_reaches_the_engine_as_the_sentence_in_its_place_and_every_other_block_unchanged(
    route: routes.Route,
) -> None:
    received = sent(route, {"model": "pool", "max_tokens": 64, "system": "Be brief.", "messages": ASKED})
    assert received["messages"] == EXPECTED
    assert received["system"] == "Be brief."
    assert "document" not in types_in(received)


def test_the_entry_names_its_defect_and_both_messages_routes() -> None:
    assert document_as_text.ENTRY.defect == "https://github.com/vllm-project/vllm/issues/58647"
    assert document_as_text.ENTRY.routes == {routes.MESSAGES, routes.COUNT_TOKENS}
