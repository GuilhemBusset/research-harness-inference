"""Token usage read from a Messages response (system design, section 6.2, steps 3 and 6).

The gateway logs the usage the engine reports for each request, and for a stream that ends badly
the tokens observed. So this parser can be asked at any moment what it has
seen so far, and whether the stream has ended cleanly.

Where Messages reports usage:

- A stream reports it in two kinds of event: `message_start` carries a usage object at
  `message.usage`, and each `message_delta` carries one at `usage`, whose counts are cumulative.
  Normally the input comes with the first and the output with the last `message_delta`. A
  `message_delta` may repeat an input field with a newer value, as Anthropic's does after a
  server-side tool ran.
- A non-streaming answer carries one usage object, at `usage`.

How the counts are made:

- The input is the sum of the three input fields of the usage object as Anthropic's reference
  defines them: `input_tokens` (not cached), `cache_creation_input_tokens` (written to the cache)
  and `cache_read_input_tokens` (read from it). Together they are the whole input, and the log
  counts all of it, cached or not (sections 11.3 and 17). The `cache_creation` object some
  responses carry is a breakdown of `cache_creation_input_tokens` and is not added again. The
  cached part (`cached_input_tokens`) is `cache_read_input_tokens`, 0 when the usage reports none:
  it is part of the input, not added to it.
- One value is kept per field. A usage object replaces the fields it carries and leaves the
  others, so the value of each field is the last one the stream reported, and a field no usage
  object carried counts as zero. A field that is `null`, or anything but a whole number that is
  not negative, is not carried.
- A stream has ended cleanly once its `message_stop` event has been read. A stream cut before it,
  or one that ends with an `error` event, has not. A non-streaming answer is clean when its body
  is a JSON object that carries a usage object.

The parser reads someone else's bytes and never raises on them: an event whose data is not a JSON
object is skipped. It sits beside the stream on `rhi_gateway.sse.SseReader` and returns counts,
never bytes, so the caller forwards each chunk exactly as it received it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from rhi_gateway.sse import SseReader

INPUT_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
"""The input fields of a usage object; their sum is the whole input."""
CACHED_FIELD = "cache_read_input_tokens"
"""The input field of the tokens read from the cache: the cached part of the input."""
OUTPUT_FIELD = "output_tokens"


@dataclass(frozen=True, slots=True)
class Usage:
    """What a response has reported so far."""

    input_tokens: int
    """The whole input: not cached, written to the cache and read from it."""
    output_tokens: int
    ended_cleanly: bool
    """Whether the response is complete: a stream's `message_stop` was read, or a whole answer."""
    cached_input_tokens: int = 0
    """The part of the input read from the cache, already counted in `input_tokens`."""


class MessagesStreamUsage:
    """Reads the usage of one stream: feed it each chunk, in order, and ask it at any moment."""

    def __init__(self) -> None:
        self._reader = SseReader()
        self._counts = _no_counts()
        self._ended = False

    def feed(self, chunk: bytes | bytearray | memoryview) -> Usage:
        """Take the next bytes of the stream and return what has been observed up to them."""
        for event in self._reader.feed(chunk):
            if event.comment:
                continue
            payload = _json_object(event.data)
            kind = event.name or payload.get("type")
            if kind == "message_start":
                message = payload.get("message")
                if isinstance(message, dict):
                    _take(self._counts, message.get("usage"))
            elif kind == "message_delta":
                _take(self._counts, payload.get("usage"))
            elif kind == "message_stop":
                self._ended = True
        return self.observed

    @property
    def observed(self) -> Usage:
        """The tokens of the events read so far, and whether the stream has ended cleanly."""
        return _usage(self._counts, ended_cleanly=self._ended)


def read_answer(body: bytes | bytearray | str) -> Usage:
    """Read the usage object of a non-streaming answer from its whole body."""
    counts = _no_counts()
    usage = _json_object(body).get("usage")
    _take(counts, usage)
    return _usage(counts, ended_cleanly=isinstance(usage, dict))


def _no_counts() -> dict[str, int]:
    return dict.fromkeys((*INPUT_FIELDS, OUTPUT_FIELD), 0)


def _usage(counts: dict[str, int], *, ended_cleanly: bool) -> Usage:
    return Usage(
        input_tokens=sum(counts[field] for field in INPUT_FIELDS),
        output_tokens=counts[OUTPUT_FIELD],
        ended_cleanly=ended_cleanly,
        cached_input_tokens=counts[CACHED_FIELD],
    )


def _take(counts: dict[str, int], usage: object) -> None:
    """Replace the counts of the fields this usage object carries."""
    if not isinstance(usage, dict):
        return
    for field in counts:
        value = usage.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts[field] = value


def _json_object(text: bytes | bytearray | str) -> dict[str, object]:
    """The JSON object this text is, or an empty one if it is anything else."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}
