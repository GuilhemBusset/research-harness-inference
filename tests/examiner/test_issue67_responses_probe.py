"""Independent checks of Responses usage accounting for issue 67."""

from __future__ import annotations

import json

from rhi_gateway.usage.responses import ResponsesStreamUsage, Usage, read_answer


def event(kind: str, response: dict[str, object]) -> bytes:
    payload = json.dumps({"type": kind, "response": response}, ensure_ascii=False)
    return f"event: {kind}\r\ndata: {payload}\r\n\r\n".encode()


def test_terminal_usage_at_every_split_and_one_byte_at_a_time() -> None:
    expected = Usage(input_tokens=327, output_tokens=19)
    prefix = event("response.created", {"usage": None})
    prefix += 'data: {"type":"response.output_text.delta","delta":"é雪"}\n\n'.encode()
    prefix += b": keep-alive\n\n"
    final = event("response.completed", {"usage": {"input_tokens": 327, "output_tokens": 19}})
    wire = prefix + final
    for split in range(len(wire) + 1):
        parser = ResponsesStreamUsage()
        parser.feed(wire[:split])
        assert parser.observed == (expected if split == len(wire) else None)
        assert parser.ended_cleanly == (split == len(wire))
        parser.feed(wire[split:])
        assert parser.observed == expected
        assert parser.ended_cleanly
    parser = ResponsesStreamUsage()
    for offset, value in enumerate(wire):
        parser.feed(bytes([value]))
        assert parser.observed == (expected if offset == len(wire) - 1 else None)
        assert parser.ended_cleanly == (offset == len(wire) - 1)


def test_nonterminal_usage_is_not_an_observation() -> None:
    parser = ResponsesStreamUsage()
    parser.feed(event("response.in_progress", {"usage": {"input_tokens": 70, "output_tokens": 2}}))
    assert parser.observed is None
    assert not parser.ended_cleanly
    final = event("response.completed", {"usage": {"input_tokens": 70, "output_tokens": 3}})
    parser.feed(final[:-1])
    assert parser.observed is None
    assert not parser.ended_cleanly
    parser.feed(final[-1:])
    assert parser.observed == Usage(input_tokens=70, output_tokens=3)
    assert parser.ended_cleanly


def test_cached_input_is_included_exactly_once_in_both_paths() -> None:
    response: dict[str, object] = {
        "usage": {
            "input_tokens": 82_000,
            "input_tokens_details": {"cached_tokens": 80_000},
            "output_tokens": 137,
            "output_tokens_details": {"reasoning_tokens": 100},
        }
    }
    expected = Usage(input_tokens=82_000, output_tokens=137)
    answer = json.dumps(response)
    assert read_answer(answer) == expected
    assert read_answer(answer.encode()) == expected
    parser = ResponsesStreamUsage()
    parser.feed(event("response.completed", response))
    assert parser.observed == expected
    assert parser.ended_cleanly


def test_failed_response_observes_usage_without_a_clean_end() -> None:
    parser = ResponsesStreamUsage()
    parser.feed(event("response.failed", {"usage": {"input_tokens": 70, "output_tokens": 3}}))
    assert parser.observed == Usage(input_tokens=70, output_tokens=3)
    assert not parser.ended_cleanly
    assert read_answer('{"usage":null}') is None
