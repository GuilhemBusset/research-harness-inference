"""The Messages blocks this endpoint cannot serve become a line of text (`rhi_gateway.formats.messages`).

Each body goes through the real Messages filter of its route, with the replacement both routes
give it and no request profile, and is read back from the bytes the filter gives to forward.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rhi_common.causes import Cause
from rhi_common.pool_config import parse_pools
from rhi_gateway import routes
from rhi_gateway.body import BodyFilter
from rhi_gateway.errors import GatewayError
from rhi_gateway.formats import messages

(POOL,) = parse_pools(
    {
        "coursework": {
            "engine": "mock",
            "instance_types": ["t3.small"],
            "prices": {"t3.small": 0.0208},
            "max_instances": 1,
            "warm_instances": 0,
            "wake": "on-demand",
            "target_concurrency": 8,
            "context_length": 262144,
            "largest_output": 64000,
        }
    }
)
ALLOW_LISTS = {routes.MESSAGES: messages.MESSAGES, routes.COUNT_TOKENS: messages.COUNT_TOKENS}
BOTH_ROUTES = pytest.mark.parametrize("route", list(ALLOW_LISTS), ids=lambda route: route.upstream_path)

TEXT = {"type": "text", "text": "Use what is below."}
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}
INLINE_DOCUMENT = {
    "type": "document",
    "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQK"},
}
SEARCH_RESULT = {
    "type": "search_result",
    "source": "https://example.com/notes",
    "title": "Notes",
    "content": [{"type": "text", "text": "A finding."}],
}
WEB_SEARCH_RESULT = {
    "type": "web_search_tool_result",
    "tool_use_id": "srvtoolu_1",
    "content": [{"type": "web_search_result", "url": "https://example.com/", "title": "Ex", "encrypted_content": "x"}],
}
URL_DOCUMENT = {"type": "document", "source": {"type": "url", "url": "https://example.com/paper.pdf"}}
KINDS = {
    "search_result": (SEARCH_RESULT, messages.SEARCH_RESULT_SENTENCE),
    "web_search_result": (WEB_SEARCH_RESULT, messages.SERVER_TOOL_SENTENCE),
    "url_document": (URL_DOCUMENT, messages.DOCUMENT_URL_SENTENCE),
}


def body_with(content: list[Any], *, in_a_tool_result: bool) -> dict[str, Any]:
    """A request whose last user message holds `content`, directly or as a tool result's content."""
    if in_a_tool_result:
        content = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}]
    return {"model": "coursework", "max_tokens": 64, "messages": [{"role": "user", "content": content}]}


def forwarded(route: routes.Route, body: dict[str, Any], *, replacement: bool = True) -> list[Any]:
    """The content of the last message as forwarded, inside its tool result when there is one."""
    body_filter = BodyFilter(ALLOW_LISTS[route], (POOL,), replacement=messages.blocks_as_text if replacement else None)
    sent = json.loads(body_filter.apply(json.dumps(body).encode(), student="s", request_id="g").body)
    content = sent["messages"][-1]["content"]
    return content[0]["content"] if content[0]["type"] == "tool_result" else content


@pytest.mark.parametrize("in_a_tool_result", [False, True], ids=["in_a_message", "in_a_tool_result"])
@pytest.mark.parametrize("kind", sorted(KINDS))
@BOTH_ROUTES
def test_each_kind_reaches_the_engine_as_its_sentence_in_its_place(
    route: routes.Route, kind: str, in_a_tool_result: bool
) -> None:
    block, sentence = KINDS[kind]
    content = forwarded(route, body_with([TEXT, block, IMAGE, INLINE_DOCUMENT], in_a_tool_result=in_a_tool_result))
    assert content == [TEXT, {"type": "text", "text": sentence}, IMAGE, INLINE_DOCUMENT]


@pytest.mark.parametrize("kind", sorted(messages.SERVER_TOOL_BLOCKS))
@BOTH_ROUTES
def test_every_listed_block_of_a_tool_anthropic_runs_is_replaced_even_with_an_object_content(
    route: routes.Route, kind: str
) -> None:
    block = {"type": kind, "tool_use_id": "srvtoolu_1", "content": {"type": f"{kind}_error", "error_code": "x"}}
    content = forwarded(route, body_with([block, TEXT], in_a_tool_result=False))
    assert content == [{"type": "text", "text": messages.SERVER_TOOL_SENTENCE}, TEXT]


@pytest.mark.parametrize("in_a_tool_result", [False, True], ids=["in_a_message", "in_a_tool_result"])
@BOTH_ROUTES
def test_an_image_url_is_still_refused(route: routes.Route, in_a_tool_result: bool) -> None:
    image = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
    with pytest.raises(GatewayError) as raised:
        forwarded(route, body_with([TEXT, image], in_a_tool_result=in_a_tool_result))
    assert raised.value.cause is Cause.REQUEST_REFUSED


@BOTH_ROUTES
def test_a_filter_given_no_replacement_still_refuses_a_document_given_by_url(route: routes.Route) -> None:
    with pytest.raises(GatewayError) as raised:
        forwarded(route, body_with([URL_DOCUMENT], in_a_tool_result=False), replacement=False)
    assert raised.value.cause is Cause.REQUEST_REFUSED


def test_the_three_sentences_are_distinct_and_hold_no_number() -> None:
    sentences = {messages.SEARCH_RESULT_SENTENCE, messages.SERVER_TOOL_SENTENCE, messages.DOCUMENT_URL_SENTENCE}
    assert len(sentences) == 3
    assert not any(character.isdigit() for sentence in sentences for character in sentence)
