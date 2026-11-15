"""Independent usage accounting and byte-preservation checks for issue 68."""

from __future__ import annotations

import json

import pytest

from rhi_gateway.usage.chat import ChatStreamUsage, Usage, read_answer

COUNTS = {"prompt_tokens":82_000,"completion_tokens":19,"prompt_tokens_details":{"cached_tokens":80_000}}
PREFIX = (
    b": alive\r\n\r\n"
    b'data: {"choices":[{"delta":{"content":"\xf0\x9f\x99\x82"}}],"usage":null}\n\n'
)
USAGE_EVENT = (
    b'id: last\r\ndata: {"choices":[],\r\n'
    + b'data: "usage":' + json.dumps(COUNTS).encode() + b"}\r\n\r\n"
)
END = b"data: [DONE]\n\n"


@pytest.mark.parametrize("asked", [False, True])
def test_counts_and_exact_bytes_at_every_split(asked: bool) -> None:
    stream = PREFIX + USAGE_EVENT + END
    expected = stream if asked else PREFIX + END
    for cut in range(len(stream) + 1):
        reader = ChatStreamUsage(client_asked_for_usage=asked)
        output = reader.feed(stream[:cut]) + reader.feed(stream[cut:]) + reader.finish()
        assert output == expected
        assert reader.observed == Usage(82_000, 19, True)
    reader = ChatStreamUsage(client_asked_for_usage=asked)
    output = b"".join(reader.feed(stream[i : i + 1]) for i in range(len(stream))) + reader.finish()
    assert output == expected
    assert reader.observed == Usage(82_000, 19, True)


def test_nonstreaming_cached_input_is_already_in_prompt_total() -> None:
    answer = json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": COUNTS})
    assert read_answer(answer.encode()) == Usage(82_000, 19, True)
    assert read_answer(answer) == Usage(82_000, 19, True)


@pytest.mark.parametrize("asked", [False, True])
def test_progress_and_disconnect_preserve_observed_counts(asked: bool) -> None:
    reader = ChatStreamUsage(client_asked_for_usage=asked)
    assert reader.observed == Usage(0, 0, False)
    reader.feed(PREFIX)
    assert reader.observed == Usage(0, 0, False)
    reader.feed(USAGE_EVENT[:-1])
    assert reader.observed == Usage(0, 0, False)
    reader.feed(USAGE_EVENT[-1:])
    assert reader.observed == Usage(82_000, 19, False)
    reader.finish()
    assert reader.observed == Usage(82_000, 19, False)


@pytest.mark.parametrize("asked", [False, True])
def test_truncated_nonusage_bytes_are_preserved(asked: bool) -> None:
    partial = PREFIX + b'data: {"choices":[{"delta":{"content":"part'
    reader = ChatStreamUsage(client_asked_for_usage=asked)
    assert reader.feed(partial) + reader.finish() == partial
    assert reader.observed == Usage(0, 0, False)
