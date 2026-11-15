"""Independent Messages usage checks for issue 66."""

from __future__ import annotations

import json

from rhi_gateway.usage.messages import MessagesStreamUsage, Usage, read_answer


def _event(name: str, payload: dict[str, object]) -> bytes:
    data = json.dumps({"type": name, **payload}, ensure_ascii=False)
    return f"event: {name}\r\ndata: {data}\r\n\r\n".encode()


def _recording() -> list[tuple[bytes, Usage]]:
    return [
        (
            _event(
                "message_start",
                {"message": {"usage": {
                    "input_tokens": 5,
                    "cache_creation_input_tokens": 11,
                    "cache_read_input_tokens": 13,
                    "output_tokens": 1,
                }}},
            ),
            Usage(29, 1, False),
        ),
        (
            _event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "é漢"}}),
            Usage(29, 1, False),
        ),
        (
            _event("message_delta", {"usage": {"output_tokens": 7, "cache_read_input_tokens": 17}}),
            Usage(33, 7, False),
        ),
        (
            _event("message_delta", {"usage": {
                "input_tokens": 9,
                "cache_creation_input_tokens": 0,
                "output_tokens": 16,
            }}),
            Usage(26, 16, False),
        ),
        (_event("ping", {}), Usage(26, 16, False)),
        (_event("message_stop", {}), Usage(26, 16, True)),
    ]


def test_independent_recording_at_every_two_chunk_boundary() -> None:
    stream = b"".join(event for event, _ in _recording())
    for cut in range(len(stream) + 1):
        parser = MessagesStreamUsage()
        parser.feed(stream[:cut])
        assert parser.feed(stream[cut:]) == Usage(26, 16, True), cut


def test_every_prefix_and_incremental_observation() -> None:
    prefix = b""
    previous = Usage(0, 0, False)
    incremental = MessagesStreamUsage()
    assert incremental.observed == previous
    for event, expected in _recording():
        prefix += event
        truncated = MessagesStreamUsage()
        assert truncated.feed(prefix) == expected
        for index, value in enumerate(event):
            observed = incremental.feed(bytes([value]))
            assert observed == (expected if index == len(event) - 1 else previous)
        assert incremental.observed == expected
        previous = expected


def test_error_retains_observed_usage_without_a_clean_end() -> None:
    parser = MessagesStreamUsage()
    for event, _ in _recording()[:3]:
        parser.feed(event)
    parser.feed(_event("error", {"error": {"type": "overloaded_error", "message": "Overloaded"}}))
    assert parser.observed == Usage(33, 7, False)


def test_nonstreaming_input_components_and_absent_fields() -> None:
    body = json.dumps({"type": "message", "usage": {
        "input_tokens": 3,
        "cache_creation_input_tokens": 9,
        "cache_read_input_tokens": 19,
        "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 5},
        "output_tokens": 6,
    }})
    assert read_answer(body.encode()) == Usage(31, 6, True)
    assert read_answer('{"usage":{"output_tokens":2}}') == Usage(0, 2, True)
    assert read_answer('{"usage":{"input_tokens":4}}') == Usage(4, 0, True)


def test_cached_input_example_independently() -> None:
    parser = MessagesStreamUsage()
    parser.feed(_event("message_start", {"message": {"usage": {
        "input_tokens": 2000,
        "cache_read_input_tokens": 80000,
    }}}))
    assert parser.observed == Usage(82_000, 0, False)
    parser.feed(_event("message_delta", {"usage": {"output_tokens": 8}}))
    assert parser.feed(_event("message_stop", {})) == Usage(82_000, 8, True)
