"""Token usage read from a Chat Completions answer (system design, section 6.2, steps 3 and 6).

The gateway settles a student's reservation to the usage the engine reports, and a stream that
ends badly is charged the tokens observed. So this parser can be asked at any moment what it has
seen so far, and whether the stream has ended cleanly.

Where Chat Completions reports usage:

- A stream reports it only when asked to, in one more chunk before `data: [DONE]`: its `choices`
  are empty and its `usage` is the usage object of the whole request. The gateway asks the engine
  for it on every stream, so a client that did not ask must not see it (below).
- A non-streaming answer carries the usage object at `usage`.

How the counts are made:

- The input is `prompt_tokens` as reported. In OpenAI's reference it already includes the tokens
  read from the cache, which `prompt_tokens_details.cached_tokens` details apart. The daily budget
  counts every input token, cached or not (sections 11.3 and 17), so the cached part is not added
  again: it is read apart, as `cached_input_tokens`, 0 when the usage details none.
- The output is `completion_tokens` as reported; it already includes the reasoning tokens that
  `completion_tokens_details` details apart.
- A usage object counts when both fields are whole numbers that are not negative. Anything else
  (`null`, as in the chunks before the last one, or a missing field) is no usage and changes
  nothing. Until a usage object has been read the counts are zero. A usage object on a chunk that
  also has choices is read like the final one, and the last one read is what is reported.
- A stream has ended cleanly once `data: [DONE]` has been read after a usage and no error: the
  answer is over and there is a reported usage to settle to. A stream cut before it has not, nor
  has one that ends without a usage.
- An error is a chunk whose `error` is not `null`, which is how Chat Completions reports one on a
  stream that has started. A stream that carried one never ends cleanly, whatever follows it: the
  counts read before it are kept, as the tokens observed, and the `[DONE]` after it changes
  nothing.

Hiding the usage chunk. This is the one place where the gateway changes the stream that reaches
the client, so `feed` returns the bytes to forward:

- When the client asked for usage, `feed` returns each chunk as it came: nothing is held back and
  nothing is removed.
- When it did not, an event is forwarded once the empty line that ends it has been read, and the
  bytes of an unfinished event wait for it. The usage chunk is an event whose data is a JSON
  object with a `usage` object and no choices. Its bytes, from the start of its first line to the
  end of the empty line that ends it, are dropped. Every other byte is forwarded as it came and in
  order. A comment line that is not inside an event, as a keep-alive is, is forwarded at once.
- When the upstream stream is over, `finish` returns the bytes still waiting: an event the stream
  never ended reaches the client as far as it got. The one exception is a usage chunk that is
  whole but for the end of its event, which stays hidden.

Only an event the stream has ended is read: `finish` changes neither the counts nor the end.

The parser reads someone else's bytes and never raises on them: an event whose data is not a JSON
object is forwarded and not read. It sits on `rhi_gateway.sse.SseReader`, which it feeds one whole
line at a time so that it knows which bytes each event is made of.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from rhi_gateway.sse import SseEvent, SseReader

DONE = "[DONE]"
"""The data of the event that ends a stream."""


@dataclass(frozen=True, slots=True)
class Usage:
    """What an answer has reported so far."""

    input_tokens: int
    """The whole input as reported, the tokens read from the cache included."""
    output_tokens: int
    ended_cleanly: bool
    """Whether the answer is complete: a stream's `[DONE]` after its usage and no error, or a whole answer."""
    cached_input_tokens: int = 0
    """The part of the input read from the cache, `prompt_tokens_details.cached_tokens`; 0 when not reported."""


class ChatStreamUsage:
    """Reads the usage of one stream and filters it for its client: feed it each chunk, in order."""

    def __init__(self, *, client_asked_for_usage: bool) -> None:
        self._client_asked = client_asked_for_usage
        self._reader = SseReader()
        self._event = bytearray()  # The whole lines of the event that has not ended yet.
        self._line = bytearray()  # The line that has not ended yet.
        self._counts: tuple[int, int, int] | None = None
        self._failed = False  # Whether a chunk has reported an error.
        self._ended = False

    def feed(self, chunk: bytes | bytearray | memoryview) -> bytes:
        """Take the next bytes of the stream and return the bytes to forward to the client."""
        if self._client_asked:
            for event in self._reader.feed(chunk):
                self._read(event)
            return bytes(chunk)
        forward = bytearray()
        *ended, rest = bytes(chunk).split(b"\n")
        for end in ended:
            line = bytes(self._line) + end + b"\n"
            self._line.clear()
            events = self._reader.feed(line)
            if line in (b"\n", b"\r\n"):
                if not any(self._read(event) for event in events):
                    forward += self._event + line
                self._event.clear()
            elif not self._event and any(event.comment for event in events):
                forward += line
            else:
                self._event += line
        self._line += rest
        return bytes(forward)

    def finish(self) -> bytes:
        """The stream is over: return the bytes of its unfinished event, which the client has not had."""
        if self._client_asked:
            return b""
        held = bytes(self._event + self._line)
        events = self._reader.feed(bytes(self._line) + b"\n\n")
        self._event.clear()
        self._line.clear()
        hidden = any(not event.comment and _is_usage_chunk(_json_object(event.data)) for event in events)
        return b"" if hidden else held

    @property
    def observed(self) -> Usage:
        """The tokens of the last usage read so far, and whether the stream has ended cleanly."""
        tokens_in, tokens_out, cached = self._counts or (0, 0, 0)
        return Usage(tokens_in, tokens_out, ended_cleanly=self._ended, cached_input_tokens=cached)

    def _read(self, event: SseEvent) -> bool:
        """Read an event the stream has ended, and say whether it is a usage chunk."""
        if event.comment:
            return False
        if event.data == DONE:
            self._ended = self._counts is not None and not self._failed
            return False
        payload = _json_object(event.data)
        self._failed = self._failed or payload.get("error") is not None
        self._counts = _counts(payload.get("usage")) or self._counts
        return _is_usage_chunk(payload)


def read_answer(body: bytes | bytearray | str) -> Usage:
    """Read the usage object of a non-streaming answer from its whole body."""
    counts = _counts(_json_object(body).get("usage"))
    tokens_in, tokens_out, cached = counts or (0, 0, 0)
    return Usage(tokens_in, tokens_out, ended_cleanly=counts is not None, cached_input_tokens=cached)


def _is_usage_chunk(payload: dict[str, object]) -> bool:
    """Whether this chunk carries a usage object and no choices."""
    return isinstance(payload.get("usage"), dict) and not payload.get("choices")


def _counts(usage: object) -> tuple[int, int, int] | None:
    """The input, output and cached input tokens of this usage object, or `None` if it is not one."""
    if not isinstance(usage, dict):
        return None
    tokens_in, tokens_out = _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))
    if tokens_in is None or tokens_out is None:
        return None
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    return tokens_in, tokens_out, cached or 0


def _count(value: object) -> int | None:
    """This value if it is a whole number that is not negative."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _json_object(text: bytes | bytearray | str) -> dict[str, object]:
    """The JSON object this text is, or an empty one if it is anything else."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}
