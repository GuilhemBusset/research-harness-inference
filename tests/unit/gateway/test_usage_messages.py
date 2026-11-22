"""Token usage read from a Messages response (`rhi_gateway.usage.messages`; section 6.2, step 6).

The recorded stream below is written by hand in the shape of Anthropic's reference: a
`message_start` whose usage carries the three input fields, a ping, a text block with a character
of two bytes and one of three, two `message_delta` events with cumulative output, `message_stop`.
Beside each event is what the parser must report once that event has been read.
"""

from __future__ import annotations

from rhi_gateway.usage.messages import MessagesStreamUsage, Usage, read_answer

RECORDED = [
    (
        (
            b'event: message_start\r\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message",'
            b'"role":"assistant","content":[],"stop_reason":null,"usage":{"input_tokens":25,'
            b'"cache_creation_input_tokens":100,"cache_read_input_tokens":300,"output_tokens":1}}}\r\n\r\n'
        ),
        Usage(425, 1, False, 300),
    ),
    (
        (
            b'event: content_block_start\r\ndata: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\r\n\r\n'
        ),
        Usage(425, 1, False, 300),
    ),
    (b'event: ping\r\ndata: {"type":"ping"}\r\n\r\n', Usage(425, 1, False, 300)),
    (
        (
            b'event: content_block_delta\r\ndata: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"caf\xc3\xa9 \xe2\x82\xac"}}\r\n\r\n'
        ),
        Usage(425, 1, False, 300),
    ),
    (
        b'event: message_delta\r\ndata: {"type":"message_delta","delta":{},"usage":{"output_tokens":9}}\r\n\r\n',
        Usage(425, 9, False, 300),
    ),
    (b'event: content_block_stop\r\ndata: {"type":"content_block_stop","index":0}\r\n\r\n', Usage(425, 9, False, 300)),
    (
        (
            b'event: message_delta\r\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":15}}\r\n\r\n'
        ),
        Usage(425, 15, False, 300),
    ),
    (b'event: message_stop\r\ndata: {"type":"message_stop"}\r\n\r\n', Usage(425, 15, True, 300)),
]
STREAM = b"".join(event for event, _ in RECORDED)
TOTALS = Usage(input_tokens=425, output_tokens=15, ended_cleanly=True, cached_input_tokens=300)


def read(*chunks: bytes) -> Usage:
    parser = MessagesStreamUsage()
    for chunk in chunks:
        parser.feed(chunk)
    return parser.observed


def test_the_recorded_stream_gives_its_totals_whatever_the_byte_boundaries() -> None:
    assert read(STREAM) == TOTALS
    assert read(*(STREAM[i : i + 1] for i in range(len(STREAM)))) == TOTALS
    different = [cut for cut in range(1, len(STREAM)) if read(STREAM[:cut], STREAM[cut:]) != TOTALS]
    assert different == []


def test_a_stream_cut_after_each_event_reports_what_was_observed_up_to_it() -> None:
    assert read() == Usage(0, 0, False)
    end, before = 0, Usage(0, 0, False)
    for event, observed in RECORDED:
        end += len(event)
        assert read(STREAM[:end]) == observed
        # An event counts once it is whole: one byte short of its end, the report is the one before.
        assert read(STREAM[: end - 1]) == before
        before = observed


def test_the_parser_reports_after_each_chunk_it_is_fed() -> None:
    parser = MessagesStreamUsage()
    assert [parser.feed(event) for event, _ in RECORDED] == [observed for _, observed in RECORDED]


def test_cached_input_is_counted_once_and_read_apart() -> None:
    stream = (
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":2000,"cache_read_input_tokens":80000,"output_tokens":1}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":40}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    assert read(stream) == Usage(input_tokens=82_000, output_tokens=40, ended_cleanly=True, cached_input_tokens=80_000)


def test_each_input_field_takes_the_last_value_the_stream_reported() -> None:
    start = (
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":2679,"cache_creation_input_tokens":50,"cache_read_input_tokens":7,"output_tokens":3}}}\n\n'
    )
    # The delta replaces one input field, carries another as null and leaves the third out.
    delta = (
        b'event: message_delta\ndata: {"type":"message_delta","usage":'
        b'{"input_tokens":10682,"cache_creation_input_tokens":null,"output_tokens":510}}\n\n'
    )
    assert read(start) == Usage(2679 + 50 + 7, 3, False, 7)
    assert read(start, delta) == Usage(10682 + 50 + 7, 510, False, 7)


def test_a_stream_that_ends_with_an_error_or_with_junk_is_not_clean() -> None:
    start, delta = RECORDED[0][0], RECORDED[4][0]
    error = b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
    assert read(start, delta, error) == Usage(425, 9, False, 300)
    junk = b'event: message_delta\ndata: {"usage":\n\nevent: message_delta\ndata: {"usage":{"output_tokens":-4}}\n\n'
    assert read(start, delta, junk, b"data: [1]\n\n: comment\n") == Usage(425, 9, False, 300)


def test_a_non_streaming_answer_gives_its_usage_object() -> None:
    answer = (
        b'{"id":"msg_1","type":"message","role":"assistant","content":[{"type":"text","text":"caf\xc3\xa9"}],'
        b'"stop_reason":"end_turn","usage":{"input_tokens":25,"cache_creation_input_tokens":100,'
        b'"cache_creation":{"ephemeral_5m_input_tokens":100},"cache_read_input_tokens":300,"output_tokens":15}}'
    )
    assert read_answer(answer) == TOTALS
    assert read_answer(answer.decode()) == TOTALS
    # A field the answer does not carry counts as zero, the cached part included.
    assert read_answer(b'{"usage":{"input_tokens":12,"output_tokens":7}}') == Usage(12, 7, True, 0)
    assert read_answer(b'{"usage":{"cache_read_input_tokens":80000}}') == Usage(80_000, 0, True, 80_000)


def test_a_body_without_a_usage_object_reports_nothing_and_is_not_clean() -> None:
    for body in (b'{"type":"error","error":{"type":"api_error"}}', b'{"usage":null}', b'{"usage":{"inp', b"[]", b""):
        assert read_answer(body) == Usage(0, 0, False)
