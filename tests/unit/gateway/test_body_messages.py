"""The body allow-list for Anthropic Messages (`rhi_gateway.formats.messages`; section 6.2, step 4).

The two requests below are written by hand: one in the shape the vLLM project's replay records for
Claude Code (evidence E28), one as a program written with the Anthropic SDK sends it. Each goes
through the real filter with the format's list and is read back from the bytes the filter gives
to forward. No filter here has a request profile: the profile is applied after this list.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from rhi_common.causes import Cause
from rhi_common.pool_config import parse_pools
from rhi_gateway.body import BodyFilter
from rhi_gateway.errors import GatewayError
from rhi_gateway.formats.messages import COUNT_TOKENS, GENERATION_ONLY, MESSAGES

MOCK = {
    "engine": "mock",
    "instance_types": ["t3.small"],
    "max_instances": 1,
    "warm_instances": 0,
    "wake": "on-demand",
    "target_concurrency": 8,
}
COURSEWORK, SMALL = parse_pools(
    {
        "coursework": {**MOCK, "context_length": 262144, "largest_output": 64000},
        "small": {**MOCK, "context_length": 131072, "largest_output": 8000},
    }
)
FILTER = BodyFilter(MESSAGES, (COURSEWORK, SMALL), default_pool=COURSEWORK)
COUNT_FILTER = BodyFilter(COUNT_TOKENS, (COURSEWORK, SMALL), default_pool=COURSEWORK)

PDF = {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQK"}
PNG = {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}
DOCUMENT = {"type": "document", "source": PDF, "title": "A paper", "context": "Read by the Read tool"}
IMAGE = {"type": "image", "source": PNG}
ASK = {"role": "user", "content": "Read the paper and plot its first table."}
UNKNOWN = "not_in_the_reference"


def raw(body: object, body_filter: BodyFilter = FILTER) -> bytes:
    return body_filter.apply(json.dumps(body).encode(), student="student-1", request_id="gateway-id").body


def forwarded(body: object, body_filter: BodyFilter = FILTER) -> dict[str, Any]:
    """What the filter gives to forward for a body, read back from its bytes."""
    return json.loads(raw(body, body_filter))


def request(*messages: object, **fields: object) -> dict[str, Any]:
    return {"model": "coursework", "max_tokens": 1024, "messages": list(messages), **fields}


def tool_result(*blocks: object) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": list(blocks)}]}


def claude_code_request() -> dict[str, Any]:
    """A turn of Claude Code's main loop, in the shape of evidence E28."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "header": {"type": "string"},
                        "choice": {"anyOf": [{"type": "string"}, {"const": 0}]},
                    },
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
    }
    tools: list[dict[str, Any]] = [
        {"name": f"Tool{number}", "description": "What the tool does.", "input_schema": schema} for number in range(25)
    ]
    tools[-1] |= {"defer_loading": True, "cache_control": {"type": "ephemeral"}}
    plot = {"type": "tool_use", "id": "toolu_3", "name": "Tool2", "input": {"command": "python plot.py"}}
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 32000,
        "stream": True,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "output_config": {"effort": "high"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "metadata": {"user_id": "session-1"},
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.281"},
            {"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        ],
        "tools": tools,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Plot the results and fix the failing test."}]},
            {  # reasoning, then two tool calls in one turn
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Two things to look at.", "signature": "signature-of-turn-1"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Tool0", "input": {"file_path": "results.csv"}},
                    {"type": "tool_use", "id": "toolu_2", "name": "Tool1", "input": {"command": "pytest -q"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a,b\n1,2\n", "is_error": False},
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_2",
                        "content": [{"type": "text", "text": "1 failed"}, IMAGE],
                        "is_error": True,
                    },
                    {"type": "text", "text": "<system-reminder>The task list is empty.</system-reminder>"},
                ],
            },
            {"role": "system", "content": "<total_tokens>41250</total_tokens>"},
            {  # thinking interleaved with the answer and the next call
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "opaque-reasoning-of-turn-2"},
                    {"type": "thinking", "thinking": "The fixture is stale.", "signature": "signature-of-turn-2"},
                    {"type": "text", "text": "The test reads an old fixture."},
                    plot,
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_3", "content": [IMAGE], "is_error": False},
                    {"type": "text", "text": "Go on.", "cache_control": {"type": "ephemeral"}},
                ],
            },
            {  # a skill loaded and a tool added mid-session
                "role": "system",
                "content": [
                    {"type": "text", "text": "<total_tokens>58800</total_tokens>"},
                    {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "Tool24"}},
                    {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": tools[0]}},
                    {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "Tool3"}},
                ],
            },
        ],
    }


def sdk_request() -> dict[str, Any]:
    """What a program written with the Anthropic SDK sends: strings where Claude Code sends blocks."""
    return {
        "model": "coursework",
        "max_tokens": 1024,
        "system": "You answer in one sentence.",
        "temperature": 0.2,
        "top_p": 0.9,
        "top_k": 40,
        "stop_sequences": ["END"],
        "metadata": {"user_id": "user-1"},
        "tools": [
            {
                "name": "get_weather",
                "description": "The weather in a city.",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
                "strict": True,
            }
        ],
        "tool_choice": {"type": "tool", "name": "get_weather", "disable_parallel_tool_use": True},
        "output_config": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
        "messages": [
            {"role": "user", "content": "What is in these two files?"},
            {"role": "assistant", "content": "Send them."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Here."},
                    IMAGE,
                    {**DOCUMENT, "citations": {"enabled": True}, "cache_control": {"type": "ephemeral"}},
                ],
            },
        ],
    }


def test_a_claude_code_request_keeps_its_fields_and_loses_the_unknown_ones() -> None:
    sent = claude_code_request()
    spoiled = copy.deepcopy(sent)
    # Two of the engine's own fields, then a field nobody lists at each level the list describes.
    spoiled |= {"chat_template_kwargs": {"enable_thinking": False}, "vllm_xargs": {"depth": 1}}
    messages = spoiled["messages"]
    for holder in (
        spoiled,
        spoiled["output_config"],
        spoiled["metadata"],
        spoiled["system"][1],
        spoiled["system"][1]["cache_control"],
        spoiled["tools"][0],
        messages[1],  # a message
        messages[1]["content"][0],  # a thinking block
        messages[2]["content"][1],  # a tool result
        messages[2]["content"][1]["content"][1],  # the image inside it
        messages[2]["content"][1]["content"][1]["source"],
        messages[6]["content"][2]["tool"],  # a tool addition
        messages[6]["content"][2]["tool"]["definition"],
    ):
        holder[UNKNOWN] = 1

    body = raw(spoiled)
    got = json.loads(body)

    assert UNKNOWN.encode() not in body
    # `context_management` is of the beta reference and of no use to the engine: it goes too.
    assert got.keys() == (sent.keys() - {"context_management"}) | {"request_id", "cache_salt"}
    for name in ("tools", "system", "messages", "thinking", "output_config", "metadata", "stream"):
        assert got[name] == sent[name], name


def test_an_sdk_request_is_forwarded_whole() -> None:
    sent = sdk_request()

    got = forwarded(sent)

    assert {name: got[name] for name in sent} == sent
    assert got.keys() == sent.keys() | {"request_id", "cache_salt"}  # and no streaming flag it did not send


@pytest.mark.parametrize("flag", [True, False])
def test_a_streaming_flag_is_kept_as_sent(flag: bool) -> None:
    assert forwarded({**sdk_request(), "stream": flag})["stream"] is flag


def test_the_output_limit_is_clamped() -> None:
    sent = claude_code_request()
    assert sent["max_tokens"] == 32000

    assert forwarded({**sent, "model": SMALL.name})["max_tokens"] == SMALL.largest_output == 8000
    # The default pool's limit is above what was asked: a clamp, and not the pool's number.
    assert forwarded(sent)["max_tokens"] == 32000


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "a-value-not-yet-published"])
@pytest.mark.parametrize(
    "thinking",
    [
        {"type": "enabled", "budget_tokens": 31999},  # above the pool's output limit, and kept
        {"type": "adaptive", "display": "omitted"},
        {"type": "disabled"},
        {"type": "a-type-not-yet-published", "depth": {"of": ["any", "shape"]}},
    ],
)
def test_the_thinking_option_and_the_effort_are_kept_whatever_they_hold(thinking: object, effort: str) -> None:
    got = forwarded(
        request(ASK, model=SMALL.name, max_tokens=32000, thinking=thinking, output_config={"effort": effort})
    )

    assert got["thinking"] == thinking
    assert got["output_config"] == {"effort": effort}


def test_earlier_reasoning_comes_back_with_its_signatures() -> None:
    reasoning = [
        {"type": "thinking", "thinking": "First the table.", "signature": "signature-of-the-thought"},
        {"type": "redacted_thinking", "data": "opaque-reasoning"},
        {"type": "text", "text": "The table is on page 2."},
    ]

    assert forwarded(request(ASK, {"role": "assistant", "content": reasoning}))["messages"][1]["content"] == reasoning


def test_a_system_message_stays_where_it_was_sent_with_its_tool_changes() -> None:
    tool = {"name": "query", "description": "Runs a query.", "input_schema": {"type": "object"}}
    changes = [
        {"type": "text", "text": "A skill was loaded."},
        {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "search"}},
        {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": tool}},
        {
            "type": "tool_removal",
            "tool": {"type": "tool_reference", "name": "fetch"},
            "cache_control": {"type": "ephemeral"},
        },
    ]
    messages = [
        {"role": "system", "content": "At the start."},
        ASK,
        {"role": "system", "content": changes},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "Thanks."},
        {"role": "system", "content": "At the end."},
    ]

    assert forwarded(request(*messages))["messages"] == messages


@pytest.mark.parametrize(
    "block",
    [
        DOCUMENT,
        IMAGE,
        {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "Plain text."}},
        {"type": "document", "source": {"type": "content", "content": [{"type": "text", "text": "A page."}, IMAGE]}},
    ],
)
def test_inline_documents_and_images_are_kept_in_a_message_and_in_a_tool_result(block: dict[str, Any]) -> None:
    messages = [{"role": "user", "content": [block]}, tool_result(block)]

    assert forwarded(request(*messages))["messages"] == messages


@pytest.mark.parametrize(
    "source",
    [{"type": "url", "url": "https://example.com/paper.pdf"}, {"type": "file", "file_id": "file_1"}],
)
def test_a_document_or_an_image_that_is_not_inline_is_refused_wherever_it_sits(source: dict[str, Any]) -> None:
    by_reference = {"type": "image", "source": source}
    inside_a_document = {"type": "document", "source": {"type": "content", "content": [by_reference]}}

    for message in (
        {"role": "user", "content": [{"type": "document", "source": source}]},
        tool_result(by_reference),
        tool_result(inside_a_document),
    ):
        with pytest.raises(GatewayError) as raised:
            forwarded(request(message))
        assert raised.value.cause is Cause.REQUEST_REFUSED


def test_what_the_profile_rewrites_is_kept() -> None:
    system = {"role": "system", "content": "<total_tokens>12000</total_tokens>"}
    in_a_message = {**DOCUMENT, "title": "Sent by the student"}
    in_a_result = {**DOCUMENT, "title": "Read by a tool"}
    sent = request(
        {"role": "user", "content": [{"type": "text", "text": "Summarise this."}, in_a_message]},
        system,
        ASK,
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}]},
        tool_result(in_a_result, IMAGE),
        output_config={"effort": "high"},
    )

    got = forwarded(sent)  # no refusal

    assert got["output_config"] == {"effort": "high"}
    assert got["messages"][1] == system
    assert [message["role"] for message in got["messages"]] == ["user", "system", "user", "assistant", "user"]
    assert got["messages"][0]["content"][1] == in_a_message
    assert got["messages"][4]["content"][0]["content"] == [in_a_result, IMAGE]
    assert got["messages"] == sent["messages"]


def test_token_counting_takes_the_same_fields_without_those_of_generation() -> None:
    sent = {**claude_code_request(), "stop_sequences": ["END"], "temperature": 0.2, "top_p": 0.9, "top_k": 40}

    generated = forwarded(sent)
    counted = forwarded(sent, COUNT_FILTER)

    assert generated.keys() - counted.keys() == GENERATION_ONLY
    assert {"max_tokens", "stream"} <= GENERATION_ONLY
    assert counted == {name: generated[name] for name in counted}
    assert {"messages", "system", "tools", "thinking", "output_config"} <= counted.keys()
