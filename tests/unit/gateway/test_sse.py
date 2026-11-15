"""The incremental reader of server-sent events (`rhi_gateway.sse`; system design, section 6.2, step 6).

The recorded stream below is written by hand in the shape of the three wire formats, with the
things a network and an engine do to one: CRLF and LF, a keep-alive comment, data on two lines and
characters of two, three and four bytes. Its events are written out beside it, so the three ways
of feeding it are compared with a known answer and not only with each other.
"""

from __future__ import annotations

from rhi_gateway.sse import SseEvent, SseReader

# Messages: named events, CRLF line ends, a ping, and a 2-, a 3- and a 4-byte character.
MESSAGES = (
    b'event: message_start\r\ndata: {"type":"message_start","message":{"usage":{"input_tokens":12}}}\r\n\r\n'
    b'event: ping\r\ndata: {"type": "ping"}\r\n\r\n'
    b'event: content_block_delta\r\ndata: {"delta":{"text":"caf\xc3\xa9 \xe2\x82\xac \xf0\x9f\x99\x82"}}\r\n\r\n'
    b'event: message_delta\r\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\r\n\r\n'
)
# Chat Completions: a comment keep-alive, unnamed events, LF line ends, the usage chunk, [DONE].
CHAT_COMPLETIONS = (
    b": keep-alive\n\n"
    b'data: {"choices":[{"delta":{"content":"na\xc3\xafve"}}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7}}\n\n'
    b"data: [DONE]\n\n"
)
# Responses: the terminal event, with its data on two lines and a field the reader skips.
RESPONSES = (
    b'event: response.completed\nid: 3\ndata: {"type":"response.completed",\ndata: "usage":{"output_tokens":7}}\n\n'
)
RECORDED = MESSAGES + CHAT_COMPLETIONS + RESPONSES

EXPECTED = [
    SseEvent("message_start", '{"type":"message_start","message":{"usage":{"input_tokens":12}}}', False),
    SseEvent("ping", '{"type": "ping"}', False),
    SseEvent("content_block_delta", '{"delta":{"text":"café € \U0001f642"}}', False),
    SseEvent("message_delta", '{"type":"message_delta","usage":{"output_tokens":7}}', False),
    SseEvent(None, "keep-alive", True),
    SseEvent(None, '{"choices":[{"delta":{"content":"naïve"}}]}', False),
    SseEvent(None, '{"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7}}', False),
    SseEvent(None, "[DONE]", False),
    SseEvent("response.completed", '{"type":"response.completed",\n"usage":{"output_tokens":7}}', False),
]


def read(*chunks: bytes) -> list[SseEvent]:
    reader = SseReader()
    return [event for chunk in chunks for event in reader.feed(chunk)]


def test_the_recorded_stream_fed_whole_gives_its_events() -> None:
    assert read(RECORDED) == EXPECTED


def test_the_recorded_stream_fed_byte_by_byte_gives_the_same_events() -> None:
    assert read(*(RECORDED[i : i + 1] for i in range(len(RECORDED)))) == EXPECTED


def test_the_recorded_stream_cut_at_every_position_gives_the_same_events() -> None:
    different = [cut for cut in range(1, len(RECORDED)) if read(RECORDED[:cut], RECORDED[cut:]) != EXPECTED]
    assert different == []


def test_an_event_has_its_name_its_data_and_is_not_a_comment() -> None:
    assert read(b"event: ping\ndata: {}\n\n") == [SseEvent(name="ping", data="{}", comment=False)]
    assert read(b"data: {}\n\n") == [SseEvent(name=None, data="{}", comment=False)]


def test_both_line_endings_give_the_same_events_even_mixed() -> None:
    expected = [SseEvent("a", "1", False), SseEvent("b", "2", False)]
    assert read(b"event: a\ndata: 1\n\nevent: b\ndata: 2\n\n") == expected
    assert read(b"event: a\r\ndata: 1\r\n\r\nevent: b\r\ndata: 2\r\n\r\n") == expected
    assert read(b"event: a\r\ndata: 1\n\r\nevent: b\ndata: 2\r\n\n") == expected
    assert read(b"event: a\r\ndata: 1\r", b"\n\r", b"\nevent: b\r\ndata: 2\r\n\r", b"\n") == expected


def test_data_on_several_lines_is_joined_with_a_line_feed() -> None:
    assert read(b"data: one\ndata: two\ndata:\ndata: four\n\n") == [SseEvent(None, "one\ntwo\n\nfour", False)]


def test_a_comment_is_reported_as_one_and_stays_out_of_the_event_around_it() -> None:
    assert read(b": keep-alive\n\n") == [SseEvent(None, "keep-alive", True)]
    assert read(b"event: a\n: between\ndata: 1\n\n") == [SseEvent(None, "between", True), SseEvent("a", "1", False)]


def test_a_character_cut_between_two_chunks_is_read_whole() -> None:
    for character in ("é", "€", "\U0001f642"):
        encoded = character.encode()
        for cut in range(1, len(encoded)):
            chunks = (b"data: " + encoded[:cut], encoded[cut:] + b"\n\n")
            assert read(*chunks) == [SseEvent(None, character, False)]


def test_an_event_is_returned_by_the_chunk_that_ends_it_and_not_before() -> None:
    reader = SseReader()
    assert reader.feed(b"event: a\ndata: 1\n") == []
    assert reader.feed(b"\n: next") == [SseEvent("a", "1", False)]
    assert reader.feed(b"\n") == [SseEvent(None, "next", True)]
    assert reader.feed(b"data: never ended\n") == []


def test_the_reader_leaves_the_bytes_it_is_given_as_they_are() -> None:
    reader = SseReader()
    chunk = bytearray(b"data: 1\n\ndata: 2")
    assert reader.feed(chunk) == [SseEvent(None, "1", False)]
    assert chunk == b"data: 1\n\ndata: 2"
    # What the reader keeps of an unfinished line is its own copy: the caller may reuse its buffer.
    chunk[:] = b"x" * len(chunk)
    assert reader.feed(b"\n\n") == [SseEvent(None, "2", False)]
