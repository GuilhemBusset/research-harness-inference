"""Independent behavioural checks for the Messages body allow-list."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from rhi_common.pool_config import parse_pools
from rhi_gateway.body import BodyFilter, Fields
from rhi_gateway.formats.messages import COUNT_TOKENS, MESSAGES


def send(payload: dict[str, Any], spec: Fields = MESSAGES) -> dict[str, Any]:
    pools = parse_pools(
        {
            "probe": {
                "engine": "mock",
                "instance_types": ["t3.small"],
                "prices": {"t3.small": 0.0208},
                "max_instances": 1,
                "warm_instances": 0,
                "wake": "on-demand",
                "target_concurrency": 1,
                "context_length": 131072,
                "largest_output": 4096,
            }
        }
    )
    body_filter = BodyFilter(spec, pools, default_pool=pools[0])
    result = body_filter.apply(json.dumps(payload).encode(), student="probe-student", request_id="probe-request")
    return json.loads(result.body)


def base(**fields: Any) -> dict[str, Any]:
    return {"model": "probe", "max_tokens": 100, "messages": [{"role": "user", "content": "Hello"}], **fields}


@pytest.mark.parametrize("value", [None, False, 7, "future", ["future"], {"type": "enabled", "budget_tokens": 9000}])
def test_reasoning_values_and_signed_history_are_unchanged(value: Any) -> None:
    history = [
        {"role": "user", "content": "Compute."},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Work", "signature": "artificial-signature"},
                {"type": "redacted_thinking", "data": "artificial-redaction"},
            ],
        },
    ]
    got = send(base(messages=history, thinking=value, output_config={"effort": value}))
    assert got["thinking"] == value
    assert got["output_config"] == {"effort": value}
    assert got["messages"] == history


def test_system_positions_and_both_tool_addition_forms() -> None:
    definition = {"name": "Lookup", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}}
    changes = [
        {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": definition}},
        {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "Lookup"}},
        {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "Lookup"}},
    ]
    history = [
        {"role": "system", "content": "First"},
        {"role": "user", "content": "One"},
        {"role": "system", "content": changes},
        {"role": "user", "content": "Two"},
        {"role": "system", "content": "Last"},
    ]
    assert send(base(messages=history))["messages"] == history


def test_profile_inputs_and_inline_media_survive_in_both_locations() -> None:
    document = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERg=="}}
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "UE5H"}}
    history = [
        {"role": "user", "content": [document, image]},
        {"role": "system", "content": "Turn reminder"},
        {"role": "user", "content": "Read both"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {}}]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": [document, image]}],
        },
    ]
    got = send(base(messages=history, output_config={"effort": "high"}))
    assert got["messages"] == history
    assert got["output_config"] == {"effort": "high"}


@pytest.mark.parametrize("requested", [1024, 32000])
@pytest.mark.parametrize("stream", [None, False, True])
def test_sdk_strings_sampling_stream_and_output_clamp(requested: int, stream: bool | None) -> None:
    sent = base(
        max_tokens=requested,
        system="Be brief",
        temperature=0.4,
        top_p=0.8,
        top_k=20,
        stop_sequences=["END"],
    )
    if stream is not None:
        sent["stream"] = stream
    got = send(sent)
    expected = {**sent, "max_tokens": min(requested, 4096)}
    assert {key: got[key] for key in expected} == expected
    assert ("stream" in got) == (stream is not None)


def test_claude_shaped_tools_history_and_unknown_fields() -> None:
    tools = [
        {
            "name": f"Lookup{index}",
            "input_schema": {"type": "object", "properties": {"rows": {"type": "array", "items": {"type": "number"}}}},
        }
        for index in range(25)
    ]
    signed = {"type": "thinking", "thinking": "Plan", "signature": "artificial-signature"}
    results = [{"type": "tool_result", "tool_use_id": f"call-{index}", "content": "Done"} for index in range(2)]
    history = [
        {"role": "user", "content": "Look up both"},
        {
            "role": "assistant",
            "content": [signed]
            + [{"type": "tool_use", "id": f"call-{index}", "name": f"Lookup{index}", "input": {}} for index in range(2)],
        },
        {"role": "user", "content": results},
        {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "artificial-redaction"}]},
    ]
    sent = base(
        max_tokens=32000,
        stream=True,
        tools=tools,
        system=[{"type": "text", "text": "Use tools", "cache_control": {"type": "ephemeral"}}],
        messages=history,
        thinking={"type": "adaptive"},
    )
    dirty = copy.deepcopy(sent)
    for holder in (dirty, dirty["tools"][0], dirty["messages"][1], dirty["messages"][1]["content"][0]):
        holder["unknown_probe_field"] = 1
    got = send(dirty)
    for key in ("tools", "system", "messages", "thinking", "stream"):
        assert got[key] == sent[key]
    assert "unknown_probe_field" not in json.dumps(got)


def test_counting_keeps_prompt_fields_and_removes_generation_fields() -> None:
    sent = base(
        max_tokens=32000,
        stream=True,
        metadata={"user_id": "probe-user"},
        stop_sequences=["END"],
        temperature=0.4,
        top_p=0.8,
        top_k=20,
        system="Instructions",
        tools=[{"name": "Read", "input_schema": {"type": "object"}}],
        tool_choice={"type": "auto"},
        thinking={"type": "enabled", "budget_tokens": 2048},
        output_config={"effort": "high"},
        cache_control={"type": "ephemeral"},
    )
    counted = send(sent, COUNT_TOKENS)
    removed = {"max_tokens", "stream", "metadata", "stop_sequences", "temperature", "top_p", "top_k"}
    assert not removed.intersection(counted)
    assert {key: counted[key] for key in sent if key not in removed} == {
        key: value for key, value in sent.items() if key not in removed
    }


@pytest.mark.parametrize("spec", [MESSAGES, COUNT_TOKENS], ids=["messages", "count_tokens"])
@pytest.mark.parametrize("citation_kind", ["config", "location"])
def test_unknown_citation_fields_are_removed(spec: Fields, citation_kind: str) -> None:
    if citation_kind == "config":
        citations: Any = {"enabled": True}
        block: dict[str, Any] = {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": "A"},
            "citations": citations,
        }
        dirty = json.loads(json.dumps(block))
        dirty["citations"]["unknown_probe_field"] = 1
    else:
        citations = [{
            "type": "char_location",
            "cited_text": "A",
            "document_index": 0,
            "document_title": "Paper",
            "start_char_index": 0,
            "end_char_index": 1,
        }]
        block = {"type": "text", "text": "A", "citations": citations}
        dirty = json.loads(json.dumps(block))
        dirty["citations"][0]["unknown_probe_field"] = 1
    got = send(base(messages=[{"role": "user", "content": [dirty]}]), spec)
    assert got["messages"][0]["content"][0] == block
