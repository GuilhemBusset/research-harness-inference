"""An incremental reader of server-sent events (system design, section 6.2, step 6).

The gateway streams a response without buffering it and reads token usage as it passes. The
network cuts a stream wherever it likes: in the middle of an event, of a line, of a character. The
usage parsers of the three wire formats sit on this one reader, which is fed the chunks as they
arrive and gives back the events each chunk completes.

The reader is a tap beside the stream, not a filter in it. `feed` takes a chunk and returns events,
never bytes: the caller forwards the very chunk it received, whenever it likes, so the reader can
neither change the bytes nor hold them back. It copies what it must keep (the end of an unfinished
line) and leaves the caller's buffer alone.

What it reads:

- A line ends at LF or at CRLF; the two may be mixed in one stream. The unfinished line is kept as
  bytes and a line is decoded only once whole, so a character cut in two is never decoded in
  halves. Bytes that are not UTF-8 become U+FFFD: a reader of someone else's stream never raises.
- `event: <name>` names the event and `data: <text>` gives its data; one space after the colon is
  not part of the value. Several `data` lines are joined with a line feed. Every other field (`id`,
  `retry`, an unknown one) is skipped.
- An empty line ends the event, which is returned if it had a name or data. An event that the
  stream never ends is never returned.
- A line that starts with a colon is a comment, as the keep-alives of the OpenAI formats are. It is
  returned as soon as its line ends, as an event of its own with the comment's text as data, and it
  takes no part in the event around it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One event of a stream, or one comment line."""

    name: str | None
    """The value of the `event` field; `None` for an event without one, and for a comment."""
    data: str
    """The `data` lines joined with a line feed, or the text of a comment; empty if there is none."""
    comment: bool
    """Whether this is a comment line rather than an event."""


class SseReader:
    """Reads one stream: feed it each chunk, in order, and it returns the events now complete."""

    def __init__(self) -> None:
        self._line = bytearray()  # The line that has not ended yet, without the chunk that will end it.
        self._name: str | None = None
        self._data: list[str] = []

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[SseEvent]:
        """Take the next bytes of the stream and return the events and comments they complete."""
        events: list[SseEvent] = []
        *ended, rest = bytes(chunk).split(b"\n")
        for end in ended:
            self._line += end
            event = self._take(self._line.removesuffix(b"\r").decode("utf-8", errors="replace"))
            self._line.clear()
            if event is not None:
                events.append(event)
        self._line += rest
        return events

    def _take(self, line: str) -> SseEvent | None:
        """Read one whole line, and return the event or the comment it completes, if any."""
        if not line:
            event = None
            if self._name is not None or self._data:
                event = SseEvent(name=self._name, data="\n".join(self._data), comment=False)
            self._name = None
            self._data = []
            return event
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if not field:
            return SseEvent(name=None, data=value, comment=True)
        if field == "event":
            self._name = value or None
        elif field == "data":
            self._data.append(value)
        return None
