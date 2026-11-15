"""Token usage read from a Responses answer (`rhi_gateway.usage.responses`; section 6.2, step 6).

The recorded stream below is written by hand in the shape of OpenAI's reference: the events before
the terminal one carry a response whose usage is still `null`, a text delta has a character of two
bytes and one of three, a comment keeps the connection alive, and `response.completed` carries the
whole response with its usage.
"""

from __future__ import annotations

from rhi_gateway.usage.responses import ResponsesStreamUsage, Usage, read_answer

USAGE = (
    b'"usage":{"input_tokens":425,"input_tokens_details":{"cached_tokens":300},"output_tokens":15,'
    b'"output_tokens_details":{"reasoning_tokens":4},"total_tokens":440}'
)
STREAM = (
    b'event: response.created\r\ndata: {"type":"response.created","sequence_number":0,'
    b'"response":{"id":"resp_1","object":"response","status":"in_progress","output":[],"usage":null}}\r\n\r\n'
    b'event: response.in_progress\r\ndata: {"type":"response.in_progress","sequence_number":1,'
    b'"response":{"id":"resp_1","object":"response","status":"in_progress","output":[],"usage":null}}\r\n\r\n'
    b'event: response.output_text.delta\r\ndata: {"type":"response.output_text.delta","sequence_number":2,'
    b'"item_id":"msg_1","output_index":0,"content_index":0,"delta":"caf\xc3\xa9 \xe2\x82\xac"}\r\n\r\n'
    b": keep-alive\r\n\r\n"
    b'event: response.output_text.done\r\ndata: {"type":"response.output_text.done","sequence_number":3,'
    b'"item_id":"msg_1","output_index":0,"content_index":0,"text":"caf\xc3\xa9 \xe2\x82\xac"}\r\n\r\n'
    b'event: response.completed\r\ndata: {"type":"response.completed","sequence_number":4,'
    b'"response":{"id":"resp_1","object":"response","status":"completed","output":[],' + USAGE + b"}}\r\n\r\n"
)
ANSWER = b'{"id":"resp_1","object":"response","status":"completed","output":[],' + USAGE + b"}"
TOTALS = Usage(input_tokens=425, output_tokens=15)


def read(*chunks: bytes) -> tuple[Usage | None, bool]:
    parser = ResponsesStreamUsage()
    for chunk in chunks:
        parser.feed(chunk)
    return parser.observed, parser.ended_cleanly


def terminal(kind: str, usage: bytes) -> bytes:
    return b'event: %b\ndata: {"type":"%b","response":{"usage":%b}}\n\n' % (kind.encode(), kind.encode(), usage)


def test_the_recorded_stream_gives_its_totals_whatever_the_byte_boundaries() -> None:
    assert read(STREAM) == (TOTALS, True)
    assert read(*(STREAM[i : i + 1] for i in range(len(STREAM)))) == (TOTALS, True)
    different = [cut for cut in range(1, len(STREAM)) if read(STREAM[:cut], STREAM[cut:]) != (TOTALS, True)]
    assert different == []


def test_a_stream_cut_before_its_terminal_event_reports_nothing_observed_and_no_clean_end() -> None:
    # The terminal event counts once it is whole: every cut, to one byte short of its end, reports nothing.
    reported = [cut for cut in range(len(STREAM)) if read(STREAM[:cut]) != (None, False)]
    assert reported == []


def test_cached_input_is_counted_once() -> None:
    usage = b'{"input_tokens":82000,"input_tokens_details":{"cached_tokens":80000},"output_tokens":40}'
    assert read(terminal("response.completed", usage)) == (Usage(input_tokens=82_000, output_tokens=40), True)
    assert read_answer(b'{"object":"response","usage":' + usage + b"}") == Usage(input_tokens=82_000, output_tokens=40)


def test_a_non_streaming_answer_gives_its_usage_object() -> None:
    assert read_answer(ANSWER) == TOTALS
    assert read_answer(ANSWER.decode()) == TOTALS
    for body in (b'{"error":{"type":"server_error"}}', b'{"usage":null}', b'{"usage":{"input_tokens":5}}', b"[]", b""):
        assert read_answer(body) is None


def test_an_answer_cut_short_at_a_limit_ends_cleanly_and_a_failed_one_does_not() -> None:
    usage = b'{"input_tokens":425,"output_tokens":15}'
    assert read(terminal("response.incomplete", usage)) == (TOTALS, True)
    # An event that comes without its `event` line is known by the type in its data.
    assert read(terminal("response.incomplete", usage).partition(b"\n")[2]) == (TOTALS, True)
    assert read(terminal("response.failed", usage)) == (TOTALS, False)
    assert read(terminal("response.failed", b"null")) == (None, False)
    error = b'event: error\ndata: {"type":"error","code":"server_error","message":"Overloaded"}\n\n'
    assert read(STREAM.rpartition(b"event: response.completed")[0], error) == (None, False)


def test_a_terminal_event_without_a_usable_usage_reports_nothing_and_no_clean_end() -> None:
    for usage in (b"null", b"{}", b'{"input_tokens":425}', b'{"input_tokens":-1,"output_tokens":15}', b"[425,15]"):
        assert read(terminal("response.completed", usage)) == (None, False)
    junk = b'event: response.completed\ndata: {"response":\n\nevent: response.completed\ndata: [1]\n\n: comment\n'
    assert read(junk) == (None, False)
