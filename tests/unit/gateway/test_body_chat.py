"""The body allow-list for OpenAI Chat Completions (`rhi_gateway.formats.chat`; section 6.2, steps 4 and 6).

The two requests below are written by hand, as a program written with the OpenAI SDK sends them:
one without tools, one with. Each goes through the format's filter and is read back from the bytes
the filter gives to forward. No filter here has a request profile unless the test gives it one.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from rhi_common.causes import Cause
from rhi_common.pool_config import parse_pools
from rhi_gateway.errors import GatewayError
from rhi_gateway.formats.chat import ChatBodyFilter, ChatForwarded

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
FILTER = ChatBodyFilter((COURSEWORK, SMALL), default_pool=COURSEWORK)

IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo=", "detail": "low"}}
ASK = {"role": "user", "content": "Plot the first table of the paper."}
UNKNOWN = "not_in_the_reference"
WRITTEN = {"request_id", "cache_salt"}  # what the shared filter adds to every body


def apply(body: object, body_filter: ChatBodyFilter = FILTER) -> ChatForwarded:
    return body_filter.apply(json.dumps(body).encode(), student="student-1", request_id="gateway-id")


def forwarded(body: object) -> dict[str, Any]:
    """What the filter gives to forward for a body, read back from its bytes."""
    return json.loads(apply(body).body)


def request(*messages: object, **fields: object) -> dict[str, Any]:
    return {"model": "coursework", "messages": list(messages), **fields}


def plain_request() -> dict[str, Any]:
    """A request with no tools: several roles, content parts, and the fields of generation."""
    return request(
        {"role": "developer", "content": "You answer in one sentence.", "name": "course"},
        {"role": "user", "content": [{"type": "text", "text": "What is in this picture?"}, IMAGE]},
        {"role": "assistant", "content": [{"type": "text", "text": "A table."}], "refusal": None},
        ASK,
        max_completion_tokens=1024,
        temperature=0.2,
        top_p=0.9,
        frequency_penalty=0.1,
        presence_penalty=-0.1,
        logit_bias={"50256": -100},
        seed=7,
        n=1,
        stop=["END"],
        logprobs=True,
        top_logprobs=3,
        verbosity="low",
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": {"type": "object", "additionalProperties": False}},
        },
    )


def tools_request() -> dict[str, Any]:
    """A turn of a tool-calling loop: the tools, a call the model made, and the tool's answer."""
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    function = {"name": "get_weather", "description": "The weather in a city.", "parameters": schema, "strict": True}
    call = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}
    return request(
        {"role": "system", "content": "Use the tools."},
        {"role": "user", "content": "What is the weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": [{"type": "text", "text": "12 degrees, rain."}]},
        tools=[{"type": "function", "function": function}],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
        parallel_tool_calls=False,
        max_tokens=512,
    )


def test_a_request_without_tools_keeps_its_fields_and_loses_the_unknown_ones() -> None:
    sent = plain_request()
    spoiled = copy.deepcopy(sent)
    # Two of the engine's own fields and one of OpenAI's own service, then a field nobody lists.
    spoiled |= {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 40, "store": True}
    picture = spoiled["messages"][1]["content"][1]
    for holder in (spoiled, spoiled["messages"][0], picture, picture["image_url"], spoiled["response_format"]):
        holder[UNKNOWN] = 1

    body = apply(spoiled).body
    got = json.loads(body)

    assert UNKNOWN.encode() not in body
    assert got.keys() == sent.keys() | WRITTEN
    assert {name: got[name] for name in sent} == sent


def test_a_request_with_tools_keeps_its_fields_and_loses_the_unknown_ones() -> None:
    sent = tools_request()
    spoiled = copy.deepcopy(sent)
    call = spoiled["messages"][2]["tool_calls"][0]
    tool = spoiled["tools"][0]
    for holder in (tool, tool["function"], call, call["function"], spoiled["tool_choice"], spoiled["messages"][3]):
        holder[UNKNOWN] = 1

    body = apply(spoiled).body
    got = json.loads(body)

    assert UNKNOWN.encode() not in body
    assert got.keys() == sent.keys() | WRITTEN
    assert {name: got[name] for name in sent} == sent
    assert got["tools"][0]["function"]["parameters"] == sent["tools"][0]["function"]["parameters"]


@pytest.mark.parametrize(
    ("options", "asked"),
    [
        ({"stream_options": {"include_usage": True}}, True),
        ({"stream_options": {"include_usage": False}}, False),
        ({"stream_options": {"include_obfuscation": False}}, False),
        ({"stream_options": None}, False),
        ({}, False),
    ],
)
def test_a_stream_leaves_with_the_usage_option_and_the_record_of_who_asked(
    options: dict[str, Any], asked: bool
) -> None:
    result = apply({**tools_request(), "stream": True, **options})
    got = json.loads(result.body)

    assert got["stream"] is True
    assert got["stream_options"] == {**(options.get("stream_options") or {}), "include_usage": True}
    assert result.client_asked_for_usage is asked
    assert result.pool is COURSEWORK


@pytest.mark.parametrize("flag", [{}, {"stream": False}, {"stream": None}])
def test_a_request_that_is_not_a_stream_is_left_as_the_list_leaves_it(flag: dict[str, Any]) -> None:
    result = apply({**plain_request(), **flag})

    assert json.loads(result.body).keys() == plain_request().keys() | flag.keys() | WRITTEN  # no stream option
    assert result.client_asked_for_usage is False


@pytest.mark.parametrize("flag", [1, "true", {"on": True}])
def test_a_stream_flag_the_engine_could_read_as_true_is_refused(flag: object) -> None:
    with pytest.raises(GatewayError) as raised:
        apply({**plain_request(), "stream": flag})
    assert raised.value.cause is Cause.REQUEST_REFUSED


def test_the_usage_option_is_written_before_the_request_profile_runs() -> None:
    seen: list[dict[str, Any]] = []

    def profile(body: dict[str, Any]) -> dict[str, Any]:
        seen.append(copy.deepcopy(body))
        return {**body, "reasoning_effort": "xhigh"}

    with_profile = ChatBodyFilter((COURSEWORK, SMALL), default_pool=COURSEWORK, profiles={COURSEWORK.name: profile})
    sent = request(ASK, stream=True, reasoning_effort="high")

    got = json.loads(apply({**sent, "model": "claude-opus-5-5"}, with_profile).body)
    other = json.loads(apply({**sent, "model": SMALL.name}, with_profile).body)

    assert seen[0]["stream_options"] == {"include_usage": True}
    assert (got["model"], got["reasoning_effort"]) == (COURSEWORK.name, "xhigh")
    # A pool with no profile gets the option too, and nothing else is rewritten.
    assert (other["stream_options"], other["reasoning_effort"]) == ({"include_usage": True}, "high")
    assert len(seen) == 1


def test_the_output_limit_is_clamped_under_both_of_its_names() -> None:
    got = forwarded(request(ASK, model=SMALL.name, max_tokens=32000, max_completion_tokens=32000))

    assert got["max_tokens"] == got["max_completion_tokens"] == SMALL.largest_output == 8000
    assert forwarded(request(ASK, max_tokens=32000))["max_tokens"] == 32000  # a clamp, not the pool's number


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max", "not-yet-published"])
def test_the_reasoning_effort_is_kept_whatever_it_holds(effort: str) -> None:
    assert forwarded(request(ASK, reasoning_effort=effort))["reasoning_effort"] == effort


def test_system_and_developer_messages_stay_where_they_were_sent() -> None:
    messages = [
        {"role": "system", "content": "At the start."},
        ASK,
        {"role": "developer", "content": [{"type": "text", "text": "A skill was loaded."}]},
        {"role": "assistant", "content": "Done."},
        {"role": "system", "content": "<total_tokens>12000</total_tokens>", "name": "harness"},
        {"role": "user", "content": "Thanks."},
        {"role": "developer", "content": "At the end."},
    ]

    assert forwarded(request(*messages))["messages"] == messages


def test_an_image_is_kept_as_inline_data_and_refused_as_an_address() -> None:
    by_address = {"type": "image_url", "image_url": {"url": "https://example.com/table.png"}}
    stored = {"type": "file", "file": {"file_id": "file-1"}}
    inline = {"type": "file", "file": {"file_data": "JVBERi0xLjQK", "filename": "paper.pdf"}}
    message = {"role": "user", "content": [{"type": "text", "text": "Read these."}, IMAGE, inline]}

    assert forwarded(request(message))["messages"] == [message]
    for part in (by_address, stored):
        with pytest.raises(GatewayError) as raised:
            forwarded(request({"role": "user", "content": [part]}))
        assert raised.value.cause is Cause.REQUEST_REFUSED


def test_what_the_profile_rewrites_is_kept() -> None:
    system = {"role": "system", "content": "<total_tokens>12000</total_tokens>"}
    first = {"role": "user", "content": "Summarise the paper."}
    sent = request(first, system, ASK, reasoning_effort="high")

    got = forwarded(sent)  # no refusal

    assert got["reasoning_effort"] == "high"
    assert got["messages"][1] == system
    assert [message["role"] for message in got["messages"]] == ["user", "system", "user"]
    assert got["messages"] == sent["messages"] == [first, system, ASK]
