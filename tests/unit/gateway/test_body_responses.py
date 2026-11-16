"""The body allow-list for OpenAI Responses (`rhi_gateway.formats.responses`; section 6.2, step 4).

The request below is written by hand in the shape Codex CLI sends (section 6.1, evidence E15):
developer and user messages, an earlier answer, a reasoning item of an earlier turn, a function
call and its output, function tools and Codex's patch tool, and reasoning options. The text in it
is invented. Each request goes through the shared filter with this format's list and is read back
from the bytes the filter gives to forward. No filter here has a request profile.
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
from rhi_gateway.formats.responses import RESPONSES

(COURSEWORK,) = parse_pools(
    {
        "coursework": {
            "engine": "mock",
            "instance_types": ["t3.small"],
            "max_instances": 1,
            "warm_instances": 0,
            "wake": "on-demand",
            "target_concurrency": 8,
            "context_length": 262144,
            "largest_output": 64000,
        }
    }
)
FILTER = BodyFilter(RESPONSES, (COURSEWORK,))
UNKNOWN = "not_in_the_reference"
IMAGE = "data:image/png;base64,iVBORw0KGgo="


def forwarded(body: object) -> dict[str, Any]:
    """What the filter gives to forward for a body, read back from its bytes."""
    raw = FILTER.apply(json.dumps(body).encode(), student="student-1", request_id="gateway-id").body
    return json.loads(raw)


def message(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


REASONING_ITEM = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [{"type": "summary_text", "text": "The file holds the notes."}],
    "content": None,
    "encrypted_content": "opaque-earlier-reasoning",
}


def codex_request() -> dict[str, Any]:
    return {
        "model": "coursework",
        "instructions": "You are a coding agent working in the student's repository.",
        "input": [
            message("developer", "Ask before running a command outside the workspace."),
            message("user", "Summarise notes.txt."),
            REASONING_ITEM,
            {"type": "function_call", "name": "shell", "arguments": '{"command":["cat","notes.txt"]}', "call_id": "c1"},
            {"type": "function_call_output", "call_id": "c1", "output": "Week 1: tokenisers."},
            {
                "type": "message",
                "role": "assistant",
                "id": "msg_1",
                "content": [{"type": "output_text", "text": "The notes cover tokenisers.", "annotations": []}],
            },
            message("user", "Now add week 2."),
        ],
        "tools": [
            {
                "type": "function",
                "name": "shell",
                "description": "Runs a command.",
                "strict": False,
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "array", "items": {"type": "string"}}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "custom",
                "name": "apply_patch",
                "description": "Edits files.",
                "format": {"type": "grammar", "syntax": "lark", "definition": "start: patch"},
            },
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "medium", "summary": "auto"},
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
        "stream": True,
        # OpenAI's stored responses, prompt cache and Codex's own field: not for this endpoint.
        "store": False,
        "prompt_cache_key": "session-1",
        "client_metadata": {"session": "1"},
    }


def test_a_codex_request_keeps_its_input_tools_and_reasoning() -> None:
    sent = codex_request()
    body = forwarded(sent)
    kept = ("instructions", "input", "tools", "tool_choice", "parallel_tool_calls", "reasoning", "text", "include")
    for name in (*kept, "stream"):
        assert body[name] == sent[name], name
    assert not {"store", "prompt_cache_key", "client_metadata"} & body.keys()


def test_unknown_fields_are_gone_at_every_level() -> None:
    sent = codex_request()
    dirty = copy.deepcopy(sent)
    dirty[UNKNOWN] = 1
    dirty["reasoning"][UNKNOWN] = 1
    dirty["input"][1][UNKNOWN] = 1  # an input item
    dirty["input"][1]["content"][0][UNKNOWN] = 1  # a content part
    dirty["input"][2]["summary"][0][UNKNOWN] = 1  # a summary part of a reasoning item
    dirty["tools"][0][UNKNOWN] = 1  # a tool
    dirty["tools"][1]["format"][UNKNOWN] = 1  # a custom tool's format
    body = forwarded(dirty)
    assert UNKNOWN not in body
    for name in ("input", "tools", "reasoning"):
        assert body[name] == sent[name], name


def test_a_tool_schema_is_data_and_kept_whole() -> None:
    sent = codex_request()
    sent["tools"][0]["parameters"]["properties"]["command"][UNKNOWN] = "kept"
    assert forwarded(sent)["tools"][0]["parameters"] == sent["tools"][0]["parameters"]


def test_what_the_profile_rewrites_is_kept() -> None:
    first, developer, second = (
        message("user", "Run the tests."),
        message("developer", "Be brief."),
        message("user", "Go."),
    )
    sent_input = [first, REASONING_ITEM, developer, second]
    body = forwarded({"model": "coursework", "input": sent_input, "reasoning": {"effort": "high"}})
    assert body["reasoning"] == {"effort": "high"}
    assert body["input"] == sent_input
    assert body["input"].index(developer) == 2
    assert body["input"][1] == REASONING_ITEM


def test_any_effort_and_a_system_message_anywhere_pass_as_sent() -> None:
    sent_input = [message("user", "Hello."), message("system", "Answer in French."), message("user", "Bye.")]
    body = forwarded({"model": "coursework", "input": sent_input, "reasoning": {"effort": "extreme-new-value"}})
    assert body["reasoning"]["effort"] == "extreme-new-value"
    assert body["input"] == sent_input


def _image_in_message(url: object, **extra: object) -> dict[str, Any]:
    part = {"type": "input_image", "image_url": url, "detail": "auto", **extra}
    return {"type": "message", "role": "user", "content": [part]}


def _image_in_tool_output(url: object, **extra: object) -> dict[str, Any]:
    part = {"type": "input_image", "image_url": url, **extra}
    return {"type": "function_call_output", "call_id": "c1", "output": [part]}


def _image_in_custom_tool_output(url: object, **extra: object) -> dict[str, Any]:
    part = {"type": "input_image", "image_url": url, **extra}
    return {"type": "custom_tool_call_output", "call_id": "c1", "output": [part]}


def _screenshot_in_computer_call_output(url: object, **extra: object) -> dict[str, Any]:
    return {
        "type": "computer_call_output",
        "call_id": "c1",
        "output": {"type": "computer_screenshot", "image_url": url, **extra},
    }


def _image_in_code_interpreter_outputs(url: object, **extra: object) -> dict[str, Any]:
    image = {"type": "image", "url": url, **extra}
    return {"type": "code_interpreter_call", "id": "ci_1", "code": "plot()", "status": "completed", "outputs": [image]}


@pytest.mark.parametrize(
    "item",
    [
        _image_in_message,
        _image_in_tool_output,
        _image_in_custom_tool_output,
        _screenshot_in_computer_call_output,
        _image_in_code_interpreter_outputs,
    ],
)
def test_an_inline_image_passes_where_the_reference_allows_one(item: Any) -> None:
    sent = item(IMAGE)
    assert forwarded({"model": "coursework", "input": [sent]})["input"] == [sent]
    refused = [item("https://example.com/plot.png")]
    if item is not _image_in_code_interpreter_outputs:  # the reference gives that image no stored file
        refused.append(item(None, file_id="file-1"))
    for body in refused:
        with pytest.raises(GatewayError) as caught:
            forwarded({"model": "coursework", "input": [body]})
        assert caught.value.cause is Cause.REQUEST_REFUSED


def test_the_client_tools_and_their_calls_keep_their_fields() -> None:
    function = {"type": "function", "name": "read", "parameters": {"type": "object"}, "strict": False, "async": True}
    tools = [{"type": "namespace", "name": "workspace", "description": "Workspace tools.", "tools": [function]}]
    call = {
        "type": "local_shell_call",
        "id": "ls_1",
        "call_id": "c1",
        "status": "completed",
        "action": {"type": "exec", "command": ["cat", "notes.txt"], "env": {}},
    }
    body = forwarded({"model": "coursework", "input": [call], "tools": tools})
    assert body["tools"] == tools
    assert body["input"] == [call]


def test_the_output_limit_is_clamped_to_the_pool() -> None:
    assert forwarded({"model": "coursework", "input": "Hi.", "max_output_tokens": 10**6})["max_output_tokens"] == 64000
