"""Token usage read from a Responses answer (system design, section 6.2, steps 3 and 6).

The gateway settles a student's reservation to the usage the engine reports, and a stream that
ends badly is charged the tokens observed and never less than the input estimate. So this parser
can be asked at any moment what it has seen so far, and whether the stream has ended cleanly.

Where Responses reports usage:

- A stream reports it once, in its terminal event: `response.completed`, `response.incomplete`
  (the answer was cut short, as at the output limit) or `response.failed`. Each carries the whole
  response object, with the usage object at `response.usage`. The events before it carry a
  response whose usage is still `null`, and only a terminal event is read. So until the terminal
  event has been read whole nothing is observed: the report is `None`, not zero tokens.
- A non-streaming answer is the response object itself, with the usage object at `usage`.

How the counts are made:

- The input is `input_tokens` as reported. In OpenAI's reference it already includes the tokens
  read from the cache, which `input_tokens_details.cached_tokens` details apart. The daily budget
  counts every input token, cached or not (sections 11.3 and 17), so the detail is not read and
  the cached part is not added again.
- The output is `output_tokens` as reported; it already includes the reasoning tokens that
  `output_tokens_details` details apart.
- A usage object counts when both fields are whole numbers that are not negative. Anything else
  (`null`, a missing field) is no usage at all: nothing is observed, and the caller charges its
  estimate.
- A stream has ended cleanly once a `response.completed` or `response.incomplete` event that
  carries a usage has been read: the answer is over and there is a reported usage to settle to. A
  stream cut before it has not, nor has one that ends with `response.failed` or an `error` event;
  the usage a `response.failed` carries is still what was observed.

The parser reads someone else's bytes and never raises on them: an event whose data is not a JSON
object is skipped. It sits beside the stream on `rhi_gateway.sse.SseReader` and returns counts,
never bytes, so the caller forwards each chunk exactly as it received it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from rhi_gateway.sse import SseReader

CLEAN_ENDS = ("response.completed", "response.incomplete")
"""The terminal events of an answer that is over, whole or cut short at a limit."""
FAILED_END = "response.failed"
"""The terminal event of an answer the engine gave up on."""


@dataclass(frozen=True, slots=True)
class Usage:
    """The usage object of a response."""

    input_tokens: int
    """The whole input as reported, the tokens read from the cache included."""
    output_tokens: int


class ResponsesStreamUsage:
    """Reads the usage of one stream: feed it each chunk, in order, and ask it at any moment."""

    def __init__(self) -> None:
        self._reader = SseReader()
        self._observed: Usage | None = None
        self._ended = False

    def feed(self, chunk: bytes | bytearray | memoryview) -> None:
        """Take the next bytes of the stream."""
        for event in self._reader.feed(chunk):
            if event.comment:
                continue
            payload = _json_object(event.data)
            kind = event.name or payload.get("type")
            if kind in CLEAN_ENDS or kind == FAILED_END:
                response = payload.get("response")
                self._observed = _usage(response.get("usage")) if isinstance(response, dict) else None
                self._ended = kind in CLEAN_ENDS and self._observed is not None

    @property
    def observed(self) -> Usage | None:
        """The tokens observed so far: `None` until a terminal event that carries a usage is read."""
        return self._observed

    @property
    def ended_cleanly(self) -> bool:
        """Whether the stream has ended with a completed or incomplete response and its usage."""
        return self._ended


def read_answer(body: bytes | bytearray | str) -> Usage | None:
    """Read the usage object of a non-streaming answer from its whole body; `None` if it has none."""
    return _usage(_json_object(body).get("usage"))


def _usage(usage: object) -> Usage | None:
    """The counts of this usage object, or `None` if it is not one."""
    if not isinstance(usage, dict):
        return None
    tokens_in, tokens_out = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    if tokens_in is None or tokens_out is None:
        return None
    return Usage(input_tokens=tokens_in, output_tokens=tokens_out)


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
