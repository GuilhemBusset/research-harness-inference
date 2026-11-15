"""Token usage read from a Chat Completions answer (`rhi_gateway.usage.chat`; section 6.2, step 6).

The recorded stream below is written by hand in the shape of OpenAI's reference for a stream that
was asked for its usage: chunks whose usage is `null`, a text delta with a character of two bytes
and one of three, a comment that keeps the connection alive, the chunk with no choices that carries
the usage, and `[DONE]`. It is made of named parts, so that the stream without its usage chunk is
those parts without that one.
"""

from __future__ import annotations

from rhi_gateway.usage.chat import ChatStreamUsage, Usage, read_answer

USAGE = (
    b'"usage":{"prompt_tokens":425,"completion_tokens":15,"total_tokens":440,'
    b'"prompt_tokens_details":{"cached_tokens":300},"completion_tokens_details":{"reasoning_tokens":4}}'
)
BEFORE = (
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,'
    b'"delta":{"role":"assistant","content":""},"finish_reason":null}],"usage":null}\n\n'
    b": keep-alive\n\n"
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,'
    b'"delta":{"content":"caf\xc3\xa9 \xe2\x82\xac"},"finish_reason":null}],"usage":null}\n\n'
    b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,'
    b'"delta":{},"finish_reason":"stop"}],"usage":null}\n\n'
)
USAGE_CHUNK = b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","model":"m","choices":[],' + USAGE + b"}\n\n"
DONE = b"data: [DONE]\n\n"
STREAM = BEFORE + USAGE_CHUNK + DONE
ANSWER = (
    b'{"id":"chatcmpl-1","object":"chat.completion","model":"m","choices":[{"index":0,'
    b'"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],' + USAGE + b"}"
)
TOTALS = Usage(input_tokens=425, output_tokens=15, ended_cleanly=True)
NOTHING = Usage(input_tokens=0, output_tokens=0, ended_cleanly=False)


def run(*chunks: bytes, asked: bool) -> tuple[bytes, Usage]:
    """What the client receives of these chunks, to the end of the stream, and what the parser reports."""
    parser = ChatStreamUsage(client_asked_for_usage=asked)
    received = b"".join([parser.feed(chunk) for chunk in chunks]) + parser.finish()
    return received, parser.observed


def chunk(usage: bytes, choices: bytes = b"[]") -> bytes:
    return b'data: {"choices":%b,"usage":%b}\n\n' % (choices, usage)


def test_the_recorded_stream_gives_its_totals_whatever_the_byte_boundaries() -> None:
    for asked, received in ((True, STREAM), (False, BEFORE + DONE)):
        assert run(STREAM, asked=asked) == (received, TOTALS)
        assert run(*(STREAM[i : i + 1] for i in range(len(STREAM))), asked=asked) == (received, TOTALS)
        cuts = range(1, len(STREAM))
        different = [cut for cut in cuts if run(STREAM[:cut], STREAM[cut:], asked=asked) != (received, TOTALS)]
        assert different == []


def test_cached_input_is_counted_once() -> None:
    usage = b'{"prompt_tokens":82000,"completion_tokens":40,"prompt_tokens_details":{"cached_tokens":80000}}'
    counted_once = Usage(input_tokens=82_000, output_tokens=40, ended_cleanly=True)
    assert run(chunk(usage), DONE, asked=False)[1] == counted_once
    assert read_answer(b'{"object":"chat.completion","choices":[],"usage":' + usage + b"}") == counted_once


def test_the_usage_chunk_is_hidden() -> None:
    assert STREAM.count(USAGE_CHUNK) == 1
    assert run(STREAM, asked=False)[0] == STREAM.replace(USAGE_CHUNK, b"") == BEFORE + DONE
    assert run(STREAM, asked=True)[0] == STREAM
    # The client that asked receives each chunk as it came, and nothing waits for the end of the stream.
    parser = ChatStreamUsage(client_asked_for_usage=True)
    pieces = [STREAM[i : i + 7] for i in range(0, len(STREAM), 7)]
    assert [parser.feed(piece) for piece in pieces] == pieces
    assert parser.finish() == b""
    # The same stream with lines that end in CRLF loses its usage chunk and nothing else.
    crlf = USAGE_CHUNK.replace(b"\n", b"\r\n")
    assert run(BEFORE + crlf + DONE, asked=False)[0] == BEFORE + DONE


def test_a_stream_that_is_cut_reports_the_tokens_so_far_and_no_clean_end() -> None:
    start, end = len(BEFORE), len(BEFORE + USAGE_CHUNK)
    for cut in range(len(STREAM)):
        for asked in (True, False):
            received, observed = run(STREAM[:cut], asked=asked)
            # The usage counts once the stream has ended its chunk, and the end is clean only after `[DONE]`.
            assert observed == (Usage(425, 15, False) if cut >= end else NOTHING)
            # A usage chunk that is whole but for the end of its event (two line feeds) is already hidden.
            hidden = not asked and cut >= end - 2
            assert received == (STREAM[:start] + STREAM[end:cut] if hidden else STREAM[:cut])


def test_a_non_streaming_answer_gives_its_usage_object() -> None:
    assert read_answer(ANSWER) == TOTALS
    assert read_answer(ANSWER.decode()) == TOTALS
    for body in (b'{"error":{"type":"server_error"}}', b'{"usage":null}', b'{"usage":{"prompt_tokens":5}}', b"[]", b""):
        assert read_answer(body) == NOTHING


def test_only_a_usage_object_without_choices_is_hidden_and_only_a_usage_makes_the_end_clean() -> None:
    usage = b'{"prompt_tokens":425,"completion_tokens":15}'
    # A stream that ends without a usage has nothing to settle to: an error and then `[DONE]`.
    error = b'data: {"error":{"message":"overloaded","type":"server_error"}}\n\n'
    assert run(BEFORE, error, DONE, asked=False) == (BEFORE + error + DONE, NOTHING)
    # A chunk that carries both choices and a usage is read, and it is not removed.
    both = chunk(usage, b'[{"index":0,"delta":{"content":"a"}}]')
    assert run(both, DONE, asked=False) == (both + DONE, TOTALS)
    # A usage object that is not usable is still a usage chunk the client did not ask for.
    for unusable in (b"{}", b'{"prompt_tokens":425}', b'{"prompt_tokens":-1,"completion_tokens":15}'):
        assert run(chunk(unusable), DONE, asked=False) == (DONE, NOTHING)
        assert run(chunk(unusable), DONE, asked=True) == (chunk(unusable) + DONE, NOTHING)
    # Data that is not a JSON object is forwarded, and a comment outside an event does not wait for one.
    junk = b'data: {"choices":\n\ndata: [1]\n\nid: 7\n\n'
    assert run(junk, asked=False) == (junk, NOTHING)
    assert ChatStreamUsage(client_asked_for_usage=False).feed(b": keep-alive\ndata: {") == b": keep-alive\n"
