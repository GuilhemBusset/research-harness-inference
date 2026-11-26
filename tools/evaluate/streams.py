"""Reads one streamed attempt on each of the three routes, and keeps every attempt of a run
(docs/design/agent-session-evaluation.md, "Reading a result").

An evaluation counts a request as answered only when its stream ended the way its route ends one:
a 200 says the answer began, not that it came whole. This module reads what the driver receives and
says, for each attempt, how it ended, when its output came and what usage it reported. It sends
nothing and keeps no clock: the driver gives it the status, the body's chunks and the time of each.

**The readers it stands on.** Events are the gateway's (`rhi_gateway.sse.SseReader`). What an event
carries of the output, and the usage a stream reports, are read as the load generator reads them
(`loadgen.output_of`, `loadgen.merge_usage`, `loadgen.tokens_of`), which is how the gateway tells
them. The events that end a stream are the gateway's own names.

**An attempt** is one request sent once. `Attempts.send` opens its `StreamReader`; the driver calls
`answered` when the status arrives, `feed` with each chunk of the body, and `end` once: when the
body is over, or, with `transport_error=True`, when the transport failed (a connection that broke,
a timeout). `end` gives the `Attempt`, and the attempt is then kept. A request sent again is
another attempt, opened with its `retry` and counted as a retry from its send: nothing replaces an
attempt.

**How an attempt ended** is one `contracts.RequestOutcome`, decided in this order:

- `http_refusal`: the status is not 2xx. Nothing of the body is read, and the answer was the
  refusal however its body ended.
- `error_event`: inside a 2xx, an event that reports a failure was received, anywhere in the body.
  That is an event named `error` or `response.failed` (`rhi_gateway.proxy.FAILURE_EVENTS`), or
  data that holds an `error`, as a chunk of Chat Completions does. An error followed by `[DONE]`
  is an error, and so is `[DONE]` followed by an error: the gateway writes its own error after a
  last event it has already passed on, when the stream it passed on reported no usage.
- `completed`: the route's own last event was read whole: `message_stop` on Messages,
  `response.completed` or `response.incomplete` on Responses (an answer cut at a limit is over),
  `data: [DONE]` on Chat Completions. The last event of another route ends nothing. A transport
  that fails after the last event takes nothing from an answer that came whole.
- `transport_error`: the driver said the transport failed, or no status arrived, and the stream
  had neither of the events above. A failure after the headers keeps its status and what was
  measured before it.
- `truncated_stream`: a 2xx whose body just ended before its last event.

After an error event nothing is read. After a last event only an error event is: no output, no
time and no usage is taken from what follows it. The status stays on the attempt beside the
outcome, `None` when none arrived, so an error inside a 200 shows both.

**An answer that is not a stream.** A harness that asks for no stream is answered with one JSON
document, and the driver says so with the status (`answered(status, stream=False)`), with the
length the answer declared when it declared one. Such an answer is `completed` when the document
came whole, `error_event` when it is a report of failure, and `truncated_stream` or
`transport_error` when it did not come whole. It came whole when it reads as one document, every
byte of its declared length arrived and the transport did not fail: a body that stops short of its
length is not whole, even when what came of it reads as a document. Its usage is the document's. It
has no time of output: nothing of it is an answer before all of it is. A 2xx read as a stream that
is not one has no last event and reads as truncated: the driver reads an answer as what the request
asked for, a stream or a document, whatever the answer says it is.

**Times** are the driver's, in milliseconds on the run's clock, under the names of
`contracts.RequestRecord`. A chunk has one time, and every event it completes has that time: events
that arrive together are not told apart, and an event cut in two is timed at the chunk that ends
it. `first_output_ms` is the first event that carries output, reasoning and a tool call's arguments
included; `first_answer_ms` the first that carries output that is not reasoning; `last_output_ms`
the last that carries any. A ping, a comment and an event with nothing in it move no time.

**Speed** is the rule of spike S5: the output tokens after the first, over the seconds from the
first output to the last, `(output_tokens - 1) / (last_output - first_output)`. It is given only
for an eligible stream: completed, at least two output tokens reported as the answer's own count
(below), and a last output later than the first. For any other it is `None`, never zero: a count
of part of the answer over the time of all of it is not its speed.

**Usage** is the `contracts.Usage` of the counts the stream reported, each field keeping the last
whole number given for it, or `None` when it reported none: missing usage is recorded, not guessed,
and does not fail an attempt. A count the format cannot hold, or a cached count above the input it
is part of, is read as not reported.

**A count that closes the answer.** A stream can report its output count more than once, and an
early one counts only what was generated by then: Messages opens with one, and an engine can report
one with every chunk. `Attempt.output_counted` says whether the output count kept is the answer's
own: one the stream reported once the answer had said it stopped, with no output after it, so that
nothing was generated past it. An answer says it stopped in its own way on each route: on Messages
an event gives its `stop_reason`; on Responses the last event carries the count itself; on Chat
Completions every choice the request asked for has given its `finish_reason` (`choices`, one unless
the driver says otherwise), or the stream sends the chunk of usage that ends it, one with no
choices after the answer's output and before `[DONE]`, whether a `finish_reason` came or not. Any
other count is still kept as the usage reported, and is not the
answer's own: one that output follows, one that came in the event that carries the output, and one
that came before the answer stopped, even when no output was seen after it. The count that opens a
Messages stream is such a one: a last token that only ends the answer, as a stop sequence does,
comes in no event of output. A document's count is the document's.

**Nothing of a body is kept.** An attempt holds labels, a status, times and counts. The text of an
answer, of an error and of a tool call is read and dropped; the reader holds only the end of an
unfinished line while the stream runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from evaluate.contracts import MAX_COUNT, RequestOutcome, Route, Usage
from loadgen import PING_EVENT, Wire, merge_usage, output_of, tokens_of, usage_in
from rhi_gateway.proxy import FAILURE_EVENTS
from rhi_gateway.sse import SseEvent, SseReader
from rhi_gateway.usage import chat, responses

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

# The load generator's name for each route's wire format: the same labels, held equal here.
_WIRES = {route: Wire(route.value) for route in Route}
# Bytes: the most of an answer that is not a stream that is held to be read. A larger one is not read.
WHOLE_ANSWER_BYTES = 8 * 1024 * 1024
# The last event of a stream that ended well, by its name, on the two routes that name their events.
STREAM_ENDS = {Route.MESSAGES: frozenset({"message_stop"}), Route.RESPONSES: frozenset(responses.CLEAN_ENDS)}
FAILURES = tuple(outcome for outcome in RequestOutcome if outcome is not RequestOutcome.COMPLETED)


@dataclass(frozen=True, kw_only=True)
class Attempt:
    """One request sent once, as the driver saw it end."""

    route: Route
    retry: int
    """How many times the request had been sent before this attempt: 0 for its first."""
    status: int | None
    """The HTTP status of the answer; `None` when none arrived."""
    outcome: RequestOutcome
    sent_ms: int
    first_output_ms: int | None
    first_answer_ms: int | None
    last_output_ms: int | None
    ended_ms: int
    usage: Usage | None
    output_counted: bool
    """Whether the output count of `usage` is the answer's own: reported once the answer had said it
    stopped, or on Chat Completions in the chunk of usage with no choices that ends the stream, with
    no output after it. `False` for any other count, and when there is none."""

    @property
    def failed(self) -> bool:
        return self.outcome is not RequestOutcome.COMPLETED

    @property
    def usage_missing(self) -> bool:
        return self.usage is None

    @property
    def output_tokens_per_second(self) -> float | None:
        """The speed of an eligible stream; `None` for any other."""
        first, last = self.first_output_ms, self.last_output_ms
        tokens = None if self.usage is None else self.usage.output_tokens
        if self.failed or not self.output_counted or tokens is None or tokens < 2:
            return None
        if first is None or last is None or last <= first:
            return None
        return (tokens - 1) * 1000 / (last - first)

    @property
    def eligible(self) -> bool:
        """Whether the stream has a speed."""
        return self.output_tokens_per_second is not None

    def measured(self) -> dict[str, Any]:
        """What this attempt gives a `contracts.RequestRecord`, under that record's names."""
        return {
            "route": self.route,
            "sent_ms": self.sent_ms,
            "first_output_ms": self.first_output_ms,
            "first_answer_ms": self.first_answer_ms,
            "last_output_ms": self.last_output_ms,
            "ended_ms": self.ended_ms,
            "outcome": self.outcome,
            "usage": self.usage,
        }


def _object(text: str) -> dict[str, Any] | None:
    """The JSON object a text is, or `None`: a stream is someone else's bytes."""
    try:
        held = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return held if isinstance(held, dict) else None


def _kind(event: SseEvent, data: Mapping[str, Any]) -> str | None:
    """The kind of an event: its name, or the `type` of its data when it has no name."""
    kind = event.name or data.get("type")
    return kind if isinstance(kind, str) else None


def _fails(kind: object, data: Mapping[str, Any]) -> bool:
    """Whether data of this kind is a report of failure, as the gateway tells one (`rhi_gateway.proxy`)."""
    return kind in FAILURE_EVENTS or data.get("error") is not None or data.get("status") == "failed"


def _reports_failure(event: SseEvent, data: Mapping[str, Any]) -> bool:
    return _fails(_kind(event, data), data)


def _ends(route: Route, event: SseEvent, data: Mapping[str, Any] | None) -> bool:
    """Whether an event is the last of a stream of this route that ended well."""
    if route is Route.CHAT_COMPLETIONS:
        return event.data == chat.DONE
    # Its name, and data that is whole, as the forwarder of the spikes asks (`forwarder.stream_end`).
    return data is not None and _kind(event, data) in STREAM_ENDS[route]


def _reported(route: Route, usage: Mapping[str, Any]) -> Usage | None:
    """The counts a stream reported, as the format holds them; `None` for no count at all."""
    whole, cached, output = (
        count if count is not None and count <= MAX_COUNT else None for count in tokens_of(_WIRES[route], usage)
    )
    if whole is not None and cached is not None and cached > whole:
        cached = None
    if whole is None and cached is None and output is None:
        return None
    return Usage(input_tokens=whole, cached_input_tokens=cached, output_tokens=output)


class StreamReader:
    """Reads one attempt: `answered` with the status, `feed` with each chunk, `end` once."""

    def __init__(
        self,
        route: Route,
        sent_ms: int,
        *,
        retry: int = 0,
        choices: int = 1,
        keep: Callable[[Attempt], None] | None = None,
    ) -> None:
        self._route, self._sent, self._retry, self._keep = route, sent_ms, retry, keep
        # The choices a Chat Completions request asked for, and those that have said why they stopped.
        self._choices = choices
        self._finished: set[object] = set()
        # Whether the answer has said it stopped: no count reported before that is its own.
        self._stopped = False
        self._status: int | None = None
        # The bytes of an answer that is not a stream, until its end; `None` for a stream.
        self._whole: bytearray | None = None
        # Of such an answer, the bytes it declared, if it declared them, and the bytes that came.
        self._length: int | None = None
        self._received = 0
        self._events = SseReader()
        self._usage: dict[str, Any] = {}
        self._first: int | None = None
        self._answer: int | None = None
        self._last: int | None = None
        # Whether the output count held was reported once the answer had stopped, after every output.
        self._counted = False
        # Set by the event that decides it. A last event can still be overruled by an error event; no other can.
        self._outcome: RequestOutcome | None = None
        self._attempt: Attempt | None = None

    def answered(self, status: int, *, stream: bool = True, length: int | None = None) -> None:
        """The answer's status has arrived. Its body is a stream of events, or, when `stream` is
        false, one JSON document, of `length` bytes when the answer declared its length."""
        self._status = status
        self._whole = None if stream else bytearray()
        self._length = None if stream else length

    def feed(self, chunk: bytes, now_ms: int) -> None:
        """Takes the next bytes of the body, received at `now_ms`."""
        if self._status is None or not 200 <= self._status < 300:
            return
        if self._whole is not None:
            self._received += len(chunk)
            if len(self._whole) <= WHOLE_ANSWER_BYTES:
                self._whole += chunk
            return
        for event in self._events.feed(chunk):
            if self._outcome is RequestOutcome.ERROR_EVENT:
                return
            if event.comment or event.name == PING_EVENT:
                continue
            data = _object(event.data)
            held = data or {}
            if self._outcome is not None:
                # The stream has ended: only an error still says something of it.
                if _reports_failure(event, held):
                    self._outcome = RequestOutcome.ERROR_EVENT
                continue
            reported = usage_in(held)
            merge_usage(self._usage, reported)
            self._stopped = self._stopped or self._stops(event, data)
            if tokens_of(_WIRES[self._route], reported)[2] is not None:
                # A count from before the answer stopped counts what was generated by then.
                self._counted = self._stopped or self._closes(data)
            carried = output_of(_WIRES[self._route], event)
            if carried is not None:
                # Output with a count, or after it: that count is not the answer's last word.
                self._counted = False
                self._first = now_ms if self._first is None else self._first
                self._last = now_ms
                if carried and self._answer is None:
                    self._answer = now_ms
            if _reports_failure(event, held):
                self._outcome = RequestOutcome.ERROR_EVENT
            elif _ends(self._route, event, data):
                self._outcome = RequestOutcome.COMPLETED

    def _stops(self, event: SseEvent, data: Mapping[str, Any] | None) -> bool:
        """Whether the answer says, with this event, that it generates nothing more."""
        if data is None:
            return False
        if self._route is Route.RESPONSES:
            return _ends(self._route, event, data)
        if self._route is Route.MESSAGES:
            delta = data.get("delta")
            return isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str)
        choices = data.get("choices")
        for choice in choices if isinstance(choices, list) else ():
            if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str):
                self._finished.add(choice.get("index", 0))
        return len(self._finished) >= self._choices

    def _closes(self, data: Mapping[str, Any] | None) -> bool:
        """Whether a Chat Completions chunk is the one of usage that closes the answer: a chunk with
        no choices once the answer's output has come. Output after it still makes it not the last."""
        if self._route is not Route.CHAT_COMPLETIONS or data is None or self._first is None:
            return False
        choices = data.get("choices")
        return choices is None or choices == []

    def end(self, now_ms: int, *, transport_error: bool = False) -> Attempt:
        """The body is over, or, with `transport_error`, the transport failed at `now_ms`: the
        attempt, the same one however often asked."""
        if self._attempt is not None:
            return self._attempt
        if self._whole is not None and len(self._whole) <= WHOLE_ANSWER_BYTES:
            document = _object(self._whole.decode("utf-8", "replace"))
            # The document came whole when it reads as one and all of its body arrived: one that
            # stops short of the length it declared can still read as a document.
            short = transport_error or (self._length is not None and self._received != self._length)
            if document is not None:
                merge_usage(self._usage, usage_in(document))
                self._counted = True
                if not short:
                    failed = _fails(document.get("type"), document)
                    self._outcome = RequestOutcome.ERROR_EVENT if failed else RequestOutcome.COMPLETED
        if self._status is not None and not 200 <= self._status < 300:
            outcome = RequestOutcome.HTTP_REFUSAL
        elif self._outcome is not None:
            outcome = self._outcome
        elif transport_error or self._status is None:
            outcome = RequestOutcome.TRANSPORT_ERROR
        else:
            outcome = RequestOutcome.TRUNCATED_STREAM
        usage = _reported(self._route, self._usage)
        self._attempt = Attempt(
            route=self._route,
            retry=self._retry,
            status=self._status,
            outcome=outcome,
            sent_ms=self._sent,
            first_output_ms=self._first,
            first_answer_ms=self._answer,
            last_output_ms=self._last,
            ended_ms=now_ms,
            usage=usage,
            output_counted=self._counted and usage is not None and usage.output_tokens is not None,
        )
        # The stream is read: the end of its last line and its usage object are not held any longer.
        self._events, self._usage, self._whole = SseReader(), {}, None
        if self._keep is not None:
            self._keep(self._attempt)
        return self._attempt


@dataclass(frozen=True, kw_only=True)
class Tally:
    """The count of a run's attempts. `attempts` is every send, so
    `attempts == completed + failed + unfinished`, and an accounting is complete when `unfinished` is 0."""

    attempts: int
    unfinished: int
    """Sends whose reader has not ended: they have no outcome yet."""
    completed: int
    failures: Mapping[RequestOutcome, int]
    """The failed attempts by cause: every cause is there, zero included."""
    retries: int
    """The sends that sent a request again, counted when sent."""
    usage_missing: int
    eligible: int
    """The streams that have a speed."""

    @property
    def failed(self) -> int:
        return sum(self.failures.values())


class Attempts:
    """Every attempt of a run, in the order they ended: failed and retried ones included."""

    def __init__(self) -> None:
        self._sent = 0
        self._retries = 0
        self._ended: list[Attempt] = []

    def send(self, route: Route, sent_ms: int, *, retry: int = 0, choices: int = 1) -> StreamReader:
        """Opens the reader of a request sent at `sent_ms`, for the `retry`-th time after its first,
        that asked for `choices` answers."""
        self._sent += 1
        self._retries += retry > 0
        return StreamReader(route, sent_ms, retry=retry, choices=choices, keep=self._ended.append)

    @property
    def ended(self) -> tuple[Attempt, ...]:
        return tuple(self._ended)

    def tally(self) -> Tally:
        ended = self._ended
        return Tally(
            attempts=self._sent,
            unfinished=self._sent - len(ended),
            completed=sum(not attempt.failed for attempt in ended),
            failures={cause: sum(attempt.outcome is cause for attempt in ended) for cause in FAILURES},
            retries=self._retries,
            usage_missing=sum(attempt.usage_missing for attempt in ended),
            eligible=sum(attempt.eligible for attempt in ended),
        )
