"""Independent checks for issue 61's shared body filter."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from rhi_common.causes import Cause
from rhi_common.pool_config import parse_pools
from rhi_gateway.body import KEEP, MAX_BODY_BYTES, BodyFilter, Fields, Items, Limit
from rhi_gateway.errors import GatewayError, WireFormat, render_json

POOL_ENTRY = {
    "engine": "mock",
    "instance_types": ["t3.small"],
    "prices": {"t3.small": 0.0208},
    "max_instances": 1,
    "warm_instances": 0,
    "wake": "on-demand",
    "target_concurrency": 2,
    "context_length": 4096,
    "largest_output": 128,
}
PRIMARY, SECONDARY = parse_pools(
    {"primary": POOL_ENTRY, "secondary": {**POOL_ENTRY, "context_length": 2048, "largest_output": 64}}
)
BLOCK = Fields(
    {
        "type": KEEP,
        "text": KEEP,
        "source": Fields({"type": KEEP, "media_type": KEEP, "data": KEEP, "url": KEEP}),
        "image_url": Fields({"url": KEEP}),
        "file_url": KEEP,
        "file_data": KEEP,
        "file_id": KEEP,
    }
)
ALLOW = Fields(
    {
        "max_tokens": Limit.LARGEST_OUTPUT,
        "context": Limit.CONTEXT_LENGTH,
        "request_id": KEEP,
        "cache_salt": KEEP,
        "reasoning": Fields({"effort": KEEP}),
        "messages": Items(Fields({"role": KEEP, "content": Items(BLOCK)})),
    }
)
FILTER = BodyFilter(ALLOW, (PRIMARY, SECONDARY), default_pool=PRIMARY)


def send(body: dict[str, Any] | bytes, body_filter: BodyFilter = FILTER, student: str = "learner-a") -> dict[str, Any]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return json.loads(body_filter.apply(raw, student=student, request_id="server-id").body)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"model":"primary","outer":{"inner":{"a":1,"\\u0061":2}}}',
        b'{"model":"primary","outer":"\xff"}',
        b'{"model":"primary",}',
    ],
)
def test_strict_parse_refusals_are_request_refused(raw: bytes) -> None:
    with pytest.raises(GatewayError) as raised:
        send(raw)
    assert raised.value.cause is Cause.REQUEST_REFUSED
    for wire in WireFormat:
        assert render_json(raised.value, wire).status == 400


class BoundedSource:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.position = 0
        self.requested_end = 0

    async def read(self, size: int) -> bytes:
        self.requested_end = max(self.requested_end, self.position + size)
        data = self.body[self.position : self.position + min(70001, size)]
        self.position += len(data)
        return data

    async def at_end(self) -> bool:
        return self.position == len(self.body)


def test_default_and_custom_byte_limits_never_read_beyond_the_limit() -> None:
    assert MAX_BODY_BYTES == 8_388_608
    for maximum in (MAX_BODY_BYTES, 47):
        body_filter = (
            BodyFilter(ALLOW, (PRIMARY,))
            if maximum == MAX_BODY_BYTES
            else BodyFilter(ALLOW, (PRIMARY,), maximum=maximum)
        )
        raw = b'{"model":"primary"}'.ljust(maximum)
        exact = BoundedSource(raw)
        result = asyncio.run(body_filter.read(exact, student="learner-a", request_id="server-id"))
        assert json.loads(result.body)["model"] == "primary"
        assert exact.position == exact.requested_end == maximum
        overflow = BoundedSource(raw + b" ")
        with pytest.raises(GatewayError) as raised:
            asyncio.run(body_filter.read(overflow, student="learner-a", request_id="server-id"))
        assert raised.value.cause is Cause.REQUEST_REFUSED
        assert overflow.position == overflow.requested_end == maximum


def test_allow_list_drops_fields_in_nested_objects_and_arrays() -> None:
    result = send(
        {
            "model": "primary",
            "discard": 1,
            "reasoning": {"effort": "max", "discard": 2},
            "messages": [{"role": "system", "discard": 3, "content": [{"text": "hello", "discard": 4}]}],
        }
    )
    assert "discard" not in result
    assert result["reasoning"] == {"effort": "max"}
    assert result["messages"] == [{"role": "system", "content": [{"text": "hello"}]}]


def test_pool_selection_clamps_and_gateway_owned_fields() -> None:
    sent = {"model": "claude-example", "max_tokens": 10000, "context": 10000, "request_id": "client-id"}
    first = send({**sent, "cache_salt": "client-salt"})
    assert first["model"] == PRIMARY.name
    assert first["max_tokens"] == PRIMARY.largest_output
    assert first["context"] == PRIMARY.context_length
    assert first["request_id"] == "server-id"
    assert first["cache_salt"] != "client-salt"
    assert first["cache_salt"] == send(sent)["cache_salt"]
    assert first["cache_salt"] != send(sent, student="learner-b")["cache_salt"]
    other = send({**sent, "model": "secondary"})
    assert other["model"] == SECONDARY.name
    assert other["max_tokens"] == SECONDARY.largest_output
    assert other["context"] == SECONDARY.context_length
    no_default = BodyFilter(ALLOW, (PRIMARY, SECONDARY))
    for refused_body in (sent, {"model": "unknown"}, {}):
        with pytest.raises(GatewayError) as raised:
            send(refused_body, no_default)
        assert raised.value.cause is Cause.REQUEST_REFUSED
    assert send({"model": "primary"}, no_default)["model"] == PRIMARY.name


@pytest.mark.parametrize("kind", ["image", "document"])
def test_inline_media_survives_but_remote_media_is_refused(kind: str) -> None:
    media_type = "image/png" if kind == "image" else "application/pdf"
    inline = {"type": kind, "source": {"type": "base64", "media_type": media_type, "data": "YQ=="}}
    sent = {"model": "primary", "messages": [{"role": "user", "content": [inline]}]}
    assert send(sent)["messages"][0]["content"] == [inline]
    remote = {"type": kind, "source": {"type": "url", "url": "https://example.com/media"}}
    sent["messages"][0]["content"] = [remote]
    with pytest.raises(GatewayError) as raised:
        send(sent)
    assert raised.value.cause is Cause.REQUEST_REFUSED


def test_profile_runs_after_shared_rewrites_for_only_its_pool() -> None:
    seen: list[dict[str, Any]] = []

    def rewrite(body: dict[str, Any]) -> dict[str, Any]:
        seen.append(body.copy())
        return {**body, "reasoning": {"effort": "low"}}

    profiled = BodyFilter(ALLOW, (PRIMARY, SECONDARY), default_pool=PRIMARY, profiles={"primary": rewrite})
    document = {"type": "document", "source": {"type": "base64", "data": "YQ=="}}
    sent = {
        "model": "claude-example",
        "max_tokens": 9999,
        "discard": True,
        "reasoning": {"effort": "max"},
        "messages": [{"role": "user", "content": []}, {"role": "system", "content": [document]}],
    }
    result = send(sent, profiled)
    assert seen == [send(sent)]
    assert result == {**seen[0], "reasoning": {"effort": "low"}}
    other = {**sent, "model": "secondary"}
    assert send(other, profiled) == send(other)
    assert len(seen) == 1


def test_forwarded_bytes_are_serialized_from_the_filtered_object() -> None:
    raw = b' { "model" : "primary", "reasoning": {"effort":"\\u0068igh"}, "discard":123 } '
    result = FILTER.apply(raw, student="learner-a", request_id="server-id")
    assert result.pool == PRIMARY
    assert result.body != raw
    assert json.loads(result.body) == {**send({"model": "primary"}), "reasoning": {"effort": "high"}}
    assert b"discard" not in result.body
