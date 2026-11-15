"""Independent checks of the Responses request allow-list for issue 64."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from rhi_common.pool_config import parse_pools
from rhi_gateway.body import BodyFilter
from rhi_gateway.formats.responses import RESPONSES

POOLS = parse_pools(
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
IMAGE = "data:image/png;base64,iVBORw0KGgo="


def forward(**fields: Any) -> dict[str, Any]:
    body = {"model": "coursework", **fields}
    result = BodyFilter(RESPONSES, POOLS).apply(
        json.dumps(body).encode(), student="student-2", request_id="probe-request"
    )
    return json.loads(result.body)


def test_codex_turn_and_unknown_fields() -> None:
    items: list[dict[str, Any]] = [
        {"role": "developer", "content": "Work in the repository."},
        {"role": "user", "content": [{"type": "input_text", "text": "Read notes.txt."}]},
        {"type": "function_call", "name": "shell", "call_id": "call-1", "arguments": '{"command":"cat notes.txt"}'},
        {"type": "function_call_output", "call_id": "call-1", "output": "Example notes."},
    ]
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": "shell",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The command."}},
                "required": ["command"],
                "additionalProperties": False,
            },
        }
    ]
    reasoning = {"effort": "medium", "summary": "auto"}
    dirty_items, dirty_tools = copy.deepcopy(items), copy.deepcopy(tools)
    dirty_items[1]["unknown_item"] = True
    dirty_items[1]["content"][0]["unknown_part"] = True
    dirty_tools[0]["unknown_tool"] = True
    actual = forward(
        input=dirty_items,
        tools=dirty_tools,
        reasoning={**reasoning, "unknown_option": True},
        unknown_top=True,
    )
    assert actual["input"] == items
    assert actual["tools"] == tools
    assert actual["reasoning"] == reasoning
    assert "unknown_top" not in actual


@pytest.mark.parametrize("effort", ["high", "future-effort"])
def test_profile_receives_effort_history_and_roles_in_place(effort: str) -> None:
    items = [
        {"role": "user", "content": "First question."},
        {
            "id": "rs_probe",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "Earlier summary."}],
            "content": [{"type": "reasoning_text", "text": "Earlier thought."}],
            "encrypted_content": "opaque-history",
            "status": "completed",
        },
        {"role": "developer", "content": "Keep the answer brief."},
        {"role": "user", "content": "Second question."},
        {"role": "system", "content": "Use plain text."},
    ]
    actual = forward(input=items, reasoning={"effort": effort})
    assert actual["reasoning"] == {"effort": effort}
    assert actual["input"] == items


@pytest.mark.parametrize("kind", ["message", "function_call_output", "custom_tool_call_output"])
def test_inline_images_in_messages_and_client_tool_results(kind: str) -> None:
    part = {"type": "input_image", "image_url": IMAGE, "detail": "auto"}
    if kind == "message":
        item = {"type": kind, "role": "user", "content": [part]}
    else:
        item = {"type": kind, "call_id": "call-1", "output": [part]}
    assert forward(input=[item])["input"] == [item]
