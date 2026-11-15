"""Independent checks of the Chat Completions request filter."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from rhi_common.pool_config import parse_pools
from rhi_gateway.formats.chat import ChatBodyFilter, ChatForwarded


def send(body: dict[str, Any]) -> ChatForwarded:
    pools = parse_pools({"probe": {
        "engine": "mock", "instance_types": ["t3.small"], "prices": {"t3.small": 0.0208}, "max_instances": 1,
        "warm_instances": 0, "wake": "on-demand", "target_concurrency": 1,
        "context_length": 131072, "largest_output": 4096,
    }})
    return ChatBodyFilter(pools).apply(json.dumps(body).encode(), student="probe", request_id="probe-id")


@pytest.mark.parametrize("with_tools", [False, True])
def test_standard_fields_survive_and_nested_unknowns_disappear(with_tools: bool) -> None:
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Hello", "name": "reader"}]
    sent: dict[str, Any] = {
        "model": "probe", "messages": messages,
        "temperature": 0.3, "top_p": 0.8, "seed": 12, "n": 1, "stop": "END",
        "presence_penalty": 0.1, "frequency_penalty": 0.2, "logit_bias": {"42": -1},
        "max_completion_tokens": 300, "logprobs": True, "top_logprobs": 2, "verbosity": "low",
        "prediction": {"type": "content", "content": [{"type": "text", "text": "Hello"}]},
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "result", "description": "Output", "schema": {"type": "object"}, "strict": True,
        }},
    }
    if with_tools:
        sent.update({
            "tools": [{"type": "function", "function": {
                "name": "lookup", "description": "Read", "parameters": {"type": "object"}, "strict": True,
            }}, {"type": "custom", "custom": {
                "name": "calc", "description": "Compute", "format": {
                    "type": "grammar", "grammar": {"syntax": "regex", "definition": "[0-9]+"},
                },
            }}],
            "tool_choice": {"type": "allowed_tools", "allowed_tools": {
                "mode": "required", "tools": [{"type": "custom", "custom": {"name": "calc"}}],
            }}, "parallel_tool_calls": False,
        })
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "custom", "custom": {"name": "calc", "input": "123"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "123"},
        ]
    dirty = copy.deepcopy(sent)

    def contaminate(value: Any) -> None:
        if isinstance(value, dict):
            for inner in list(value.values()):
                contaminate(inner)
            value["unknown_probe_field"] = True
        elif isinstance(value, list):
            for inner in value:
                contaminate(inner)

    # JSON Schema and logit_bias are opaque user data, not allow-list objects.
    schema = dirty["response_format"]["json_schema"].pop("schema")
    bias = dirty.pop("logit_bias")
    parameters = dirty["tools"][0]["function"].pop("parameters") if with_tools else None
    contaminate(dirty)
    dirty["response_format"]["json_schema"]["schema"] = schema
    dirty["logit_bias"] = bias
    if with_tools:
        dirty["tools"][0]["function"]["parameters"] = parameters
    got = json.loads(send(dirty).body)
    assert {key: got[key] for key in sent} == sent
    assert set(got) == set(sent) | {"cache_salt", "request_id"}
    assert "unknown_probe_field" not in json.dumps(got)


@pytest.mark.parametrize("effort", ["high", "future-value", None, {"future": 1}])
def test_effort_history_order_and_inline_image_are_preserved(effort: Any) -> None:
    history = [
        {"role": "user", "content": "First"},
        {"role": "system", "content": "Middle"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {
            "url": "data:image/png;base64,UE5H", "detail": "auto",
        }}]},
        {"role": "developer", "content": "Last"},
    ]
    got = json.loads(send({"model": "probe", "messages": history, "reasoning_effort": effort}).body)
    assert got["messages"] == history
    assert got["reasoning_effort"] == effort


@pytest.mark.parametrize("options", [None, {}, {"include_usage": False}, {"include_usage": True}])
def test_stream_usage_is_forced_and_original_preference_is_recorded(options: dict[str, Any] | None) -> None:
    sent: dict[str, Any] = {"model": "probe", "messages": [{"role": "user", "content": "Hello"}], "stream": True}
    if options is not None:
        sent["stream_options"] = {**options, "include_obfuscation": False}
    result = send(sent)
    got = json.loads(result.body)
    assert got["stream_options"]["include_usage"] is True
    assert result.client_asked_for_usage is (options is not None and options.get("include_usage") is True)
    if options is not None:
        assert got["stream_options"]["include_obfuscation"] is False
