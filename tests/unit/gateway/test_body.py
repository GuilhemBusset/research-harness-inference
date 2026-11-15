"""The body filter (`rhi_gateway.body`; system design, section 6.2, step 4).

The allow-list below is written for these tests in the shape the three wire formats share: it is
no format's own. Bodies are sent as a client would send them and read back from the bytes the
filter gives to forward.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from rhi_common.causes import Cause
from rhi_common.pool_config import parse_pools
from rhi_gateway.body import KEEP, MAX_BODY_BYTES, BodyFilter, Fields, Items, Limit, read_body
from rhi_gateway.errors import GatewayError

MOCK = {
    "engine": "mock",
    "instance_types": ["t3.small"],
    "prices": {"t3.small": 0.0208},
    "max_instances": 1,
    "warm_instances": 0,
    "wake": "on-demand",
    "target_concurrency": 8,
}
COURSEWORK, UPGRADE = parse_pools(
    {
        "coursework": {**MOCK, "context_length": 262144, "largest_output": 32000},
        "upgrade": {**MOCK, "context_length": 131072, "largest_output": 8000},
    }
)

BLOCK = Fields(
    {
        "type": KEEP,
        "text": KEEP,
        # Messages: an image or a document, inline or by reference.
        "source": Fields({"type": KEEP, "media_type": KEEP, "data": KEEP, "url": KEEP, "file_id": KEEP}),
        # Chat Completions and Responses: an object with a URL, or the URL itself.
        "image_url": Fields({"url": KEEP}),
        "file_url": KEEP,
        "file_data": KEEP,
        "file_id": KEEP,
        # A tool call's arguments: data, whatever it holds.
        "input": KEEP,
    }
)
ALLOW_LIST = Fields(
    {
        "model": KEEP,
        "max_tokens": Limit.LARGEST_OUTPUT,
        "truncate_prompt_tokens": Limit.CONTEXT_LENGTH,
        "reasoning": Fields({"effort": KEEP}),
        "messages": Items(Fields({"role": KEEP, "content": Items(BLOCK)})),
    }
)
POOLS = (COURSEWORK, UPGRADE)
FILTER = BodyFilter(ALLOW_LIST, POOLS, default_pool=COURSEWORK)

URL = "https://example.com/figure"
INLINE = "data:image/png;base64,aGVsbG8="


def forwarded(body: object, body_filter: BodyFilter = FILTER, student: str = "student-1") -> dict[str, Any]:
    """What the filter gives to forward for a body, read back from its bytes."""
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return json.loads(body_filter.apply(raw, student=student, request_id="gateway-id").body)


def refused(call: Callable[[], object]) -> bool:
    with pytest.raises(GatewayError) as raised:
        call()
    return raised.value.cause is Cause.REQUEST_REFUSED


def message(*blocks: object) -> dict[str, Any]:
    return {"model": "coursework", "messages": [{"role": "user", "content": list(blocks)}]}


class Source:
    """A body handed out in pieces. It records how far into the body it was ever asked to go."""

    def __init__(self, data: bytes, piece: int = 65536) -> None:
        self.data = data
        self.piece = piece
        self.handed_out = 0
        self.reach = 0

    async def read(self, size: int) -> bytes:
        self.reach = max(self.reach, self.handed_out + size)
        data = self.data[self.handed_out : self.handed_out + min(size, self.piece)]
        self.handed_out += len(data)
        return data

    async def at_end(self) -> bool:
        return self.handed_out == len(self.data)


def read(source: Source, body_filter: BodyFilter = FILTER) -> dict[str, Any]:
    return json.loads(asyncio.run(body_filter.read(source, student="student-1", request_id="gateway-id")).body)


@pytest.mark.parametrize(
    "body",
    [
        b'{"model":"coursework","a":{"b":{"k":1,"k":2}}}',  # in the object at depth three
        b'{"model":"coursework","model":"coursework"}',
        b'{"model":"coursework","messages":[{"role":"user","content":[{"input":{"k":1,"k":1}}]}]}',
    ],
)
def test_a_duplicate_key_at_any_depth_is_refused(body: bytes) -> None:
    assert refused(lambda: forwarded(body))


@pytest.mark.parametrize(
    "body",
    [
        b'{"model":"coursework","text":"caf\xe9"}',  # Latin-1, not UTF-8
        b"\xff\xfe{\x00}\x00",  # UTF-16
        b'{"model":"coursework"',
        b'["coursework"]',
        b'{"model":"coursework","max_tokens":NaN}',
        b'{"model":"coursework","max_tokens":1e999}',
        b"[" * 100_000 + b"]" * 100_000,
    ],
)
def test_a_body_that_is_not_one_utf8_json_object_is_refused(body: bytes) -> None:
    assert refused(lambda: forwarded(body))


def test_a_body_above_the_maximum_is_refused_once_the_maximum_is_read_and_no_more() -> None:
    start = b'{"model":"coursework","text":"'
    at_the_maximum = start + b"a" * (MAX_BODY_BYTES - len(start) - 2) + b'"}'
    assert MAX_BODY_BYTES == 8_388_608 == len(at_the_maximum)
    assert read(Source(at_the_maximum))["model"] == "coursework"

    # One byte more. The first 8,388,608 bytes are a body the filter takes, so a filter that read
    # them and forwarded what it had would not refuse. In pieces that end on the maximum, and not.
    for piece in (65536, 100_000):
        above = Source(at_the_maximum + b" ", piece)
        assert refused(lambda above=above: read(above))
        assert above.handed_out == MAX_BODY_BYTES
        assert above.reach == MAX_BODY_BYTES

    # The maximum is the filter's parameter.
    small = BodyFilter(ALLOW_LIST, POOLS, maximum=64)
    at_64 = b'{"model":"coursework"}'.ljust(64)
    assert read(Source(at_64, 5), small) == forwarded({"model": "coursework"})
    long = Source(at_64 + b" ", 5)
    assert refused(lambda: read(long, small))
    assert long.handed_out == long.reach == 64
    assert refused(lambda: forwarded(long.data, small))

    class Greedy(Source):
        async def read(self, size: int) -> bytes:
            return self.data

    assert refused(lambda: asyncio.run(read_body(Greedy(b"a" * 11), 10)))


def test_an_unlisted_field_is_absent_from_the_forwarded_body() -> None:
    body = {
        "model": "coursework",
        "unlisted": {"model": "coursework"},
        "reasoning": {"effort": "low", "unlisted": 1},
        "messages": [
            {
                "role": "user",
                "unlisted": [1],
                "content": [{"type": "text", "text": "hello", "unlisted": None, "input": {"anything": {"at": "all"}}}],
            },
            # Where the allow-list describes an array or an object, a string holds nothing to drop.
            {"role": "user", "content": "hello"},
        ],
    }
    assert forwarded(body) == {
        "model": "coursework",
        "reasoning": {"effort": "low"},
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello", "input": {"anything": {"at": "all"}}}]},
            {"role": "user", "content": "hello"},
        ],
        "request_id": "gateway-id",
        "cache_salt": forwarded(body)["cache_salt"],
    }
    # A container of the other kind would carry fields that the allow-list does not describe.
    assert refused(lambda: forwarded({"model": "coursework", "reasoning": [{"unlisted": 1}]}))
    assert refused(lambda: forwarded({"model": "coursework", "messages": {"unlisted": 1}}))


def test_shared_rewrites() -> None:
    # The model name: a pool's own is kept, a claude- name is the default pool's, any other is refused.
    claude = {"model": "claude-sonnet-4-5", "max_tokens": 64}
    assert FILTER.apply(json.dumps(claude).encode(), student="student-1", request_id="gateway-id").pool is COURSEWORK
    assert forwarded(claude)["model"] == "coursework"
    assert forwarded({"model": "upgrade"})["model"] == "upgrade"
    without_default = BodyFilter(ALLOW_LIST, POOLS)
    assert refused(lambda: forwarded(claude, without_default))
    assert forwarded({"model": "upgrade"}, without_default)["model"] == "upgrade"
    for name in ("gpt-5", "Coursework", "", None, ["coursework"]):
        assert refused(lambda name=name: forwarded({"model": name}))
    assert refused(lambda: forwarded({"max_tokens": 64}))

    # Numbers: clamped to the limits of the pool the request is for.
    assert forwarded({"model": "coursework", "max_tokens": 1_000_000})["max_tokens"] == COURSEWORK.largest_output
    assert forwarded({"model": "upgrade", "max_tokens": 1_000_000})["max_tokens"] == UPGRADE.largest_output
    assert forwarded({"model": "upgrade", "max_tokens": 1e9})["max_tokens"] == UPGRADE.largest_output
    assert forwarded({"model": "upgrade", "max_tokens": 512})["max_tokens"] == 512
    beyond = {"model": "upgrade", "truncate_prompt_tokens": UPGRADE.context_length + 1}
    assert forwarded(beyond)["truncate_prompt_tokens"] == UPGRADE.context_length
    assert forwarded({"model": "upgrade", "max_tokens": None})["max_tokens"] is None
    for no_number in ("1000000", True, [1_000_000]):
        assert refused(lambda no_number=no_number: forwarded({"model": "upgrade", "max_tokens": no_number}))

    # The cache salt is the student's, and the request ID the gateway's, whatever the client sent.
    sent = {"model": "coursework", "cache_salt": "chosen by the client", "request_id": "chosen by the client"}
    first, again, second = forwarded(sent), forwarded(sent), forwarded(sent, student="student-2")
    assert first["cache_salt"] == again["cache_salt"] != second["cache_salt"]
    assert "chosen by the client" not in (first["cache_salt"], second["cache_salt"], first["request_id"])
    assert first["request_id"] == "gateway-id"
    assert "student-1" not in first["cache_salt"]

    # Media: an image and a document by URL are refused, and the same two inline pass.
    image_by_url = {"type": "image", "source": {"type": "url", "url": URL}}
    document_by_url = {"type": "document", "source": {"type": "url", "url": URL}}
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}}
    document = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "aGVsbG8="}}
    assert refused(lambda: forwarded(message(image_by_url)))
    assert refused(lambda: forwarded(message(document_by_url)))
    assert forwarded(message(image, document))["messages"][0]["content"] == [image, document]
    # The same in the two OpenAI formats, and a file the engine would have to look up.
    for reference in (
        {"type": "image_url", "image_url": {"url": URL}},
        {"type": "input_image", "image_url": URL},
        {"image_url": URL},
        {"type": "input_file", "file_url": URL},
        {"type": "input_file", "file_id": "file-1"},
        {"type": "document", "source": {"type": "file", "file_id": "file-1"}},
        {"type": "image_url", "image_url": {"url": " " + INLINE}},
    ):
        assert refused(lambda reference=reference: forwarded(message(reference)))
    inline = [
        {"type": "image_url", "image_url": {"url": INLINE}},
        {"type": "input_image", "image_url": INLINE},
        {"type": "input_file", "file_data": "aGVsbG8=", "file_id": None},
        # A tool call's arguments are data: a URL in them is no media reference.
        {"type": "tool_use", "input": {"url": URL, "file_id": "file-1"}},
    ]
    assert forwarded(message(*inline))["messages"][0]["content"] == inline
    # Nesting that the parser takes and a walk cannot follow is a refusal too.
    deep = json.loads('{"url":' * 5000 + '"' + INLINE + '"' + "}" * 5000)
    assert refused(lambda: forwarded(message({"type": "image_url", "image_url": deep})))


def test_the_profile_step_sees_the_filtered_body() -> None:
    seen: list[dict[str, Any]] = []

    def rewrite(body: dict[str, Any]) -> dict[str, Any]:
        seen.append(json.loads(json.dumps(body)))
        return {**body, "reasoning": {"effort": "xhigh"}}

    with_profile = BodyFilter(ALLOW_LIST, POOLS, default_pool=COURSEWORK, profiles={"coursework": rewrite})
    sent = {"model": "claude-opus-4-5", "max_tokens": 1_000_000, "unlisted": 1, "reasoning": {"effort": "high"}}

    result = forwarded(sent, with_profile)
    # The rewrite got the body after every shared rewrite, and what it returned is what is sent.
    assert seen == [forwarded(sent)]
    assert seen[0]["model"] == "coursework"
    assert seen[0]["max_tokens"] == COURSEWORK.largest_output
    assert seen[0]["request_id"] == "gateway-id"
    assert "unlisted" not in seen[0]
    assert seen[0]["reasoning"] == {"effort": "high"}
    assert result == {**seen[0], "reasoning": {"effort": "xhigh"}}

    # Another pool has no profile: its body is the filtered body, and the rewrite is not called.
    other = {**sent, "model": "upgrade"}
    assert forwarded(other, with_profile) == forwarded(other)
    assert forwarded(other, with_profile)["reasoning"] == {"effort": "high"}
    assert len(seen) == 1


def test_what_is_forwarded_is_the_gateways_own_serialization() -> None:
    sent = '{ "unlisted" : 1,\n  "reasoning" : { "effort" : "café \\u20ac" },\t"model" : "claude-haiku-4-5" }'
    body = FILTER.apply(sent.encode(), student="student-1", request_id="gateway-id").body
    kept = json.loads(body)
    assert kept == {**forwarded({"model": "coursework"}), "reasoning": {"effort": "café €"}}
    assert body == json.dumps(kept, separators=(",", ":")).encode()
    assert body.isascii()
    assert b"unlisted" not in body
