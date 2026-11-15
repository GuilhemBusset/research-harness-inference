"""Independent examination of the byte-fed SSE reader."""

from rhi_gateway.sse import SseReader


def unpack(events):
    return [(event.name, event.data, event.comment) for event in events]


def test_events_are_available_on_the_feed_that_completes_them():
    reader = SseReader()
    assert reader.feed(b"event: usage\r\ndata: first\n") == []
    assert unpack(reader.feed(b": pulse\r\n")) == [(None, "pulse", True)]
    assert reader.feed(b"data: second\r\n\r") == []
    assert unpack(reader.feed(b"\ndata:\n\n")) == [
        ("usage", "first\nsecond", False),
        (None, "", False),
    ]
    assert reader.feed(b"data: incomplete") == []
    assert reader.feed(b"") == []


def test_input_storage_is_unchanged_and_can_be_reused_immediately():
    reader = SseReader()
    original = b"data: sent\n\nevent: usage\ndata: caf\xc3"
    storage = bytearray(original)
    view = memoryview(storage)
    assert unpack(reader.feed(view)) == [(None, "sent", False)]
    assert bytes(storage) == original
    storage[:] = b"x" * len(storage)
    assert unpack(reader.feed(b"\xa9\n\n")) == [("usage", "caf\u00e9", False)]


def test_independent_stream_at_every_split_and_fixed_chunk_width():
    stream = (
        "event: usage\r\n"
        "data: caf\u00e9 \u20ac \U0001f642\n"
        ": heartbeat\r\n"
        "data: second\r\n"
        "\r\n"
        "data:\n\n"
        "event: complete\ndata: [DONE]\n\n"
    ).encode("utf-8")
    expected = [
        (None, "heartbeat", True),
        ("usage", "caf\u00e9 \u20ac \U0001f642\nsecond", False),
        (None, "", False),
        ("complete", "[DONE]", False),
    ]

    def check(chunks):
        reader = SseReader()
        actual = []
        for chunk in chunks:
            actual.extend(unpack(reader.feed(chunk)))
        assert actual == expected
        assert reader.feed(b"") == []

    check([stream])
    for cut in range(len(stream) + 1):
        check([stream[:cut], b"", stream[cut:]])
    for width in range(1, len(stream) + 1):
        check([stream[start : start + width] for start in range(0, len(stream), width)])
