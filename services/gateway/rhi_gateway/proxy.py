"""Forward a filtered request to its pool and stream the answer back (system design, sections 3 and 6.2).

This is the gateway's one hop to the engine: steps 1, 6 and 7 of section 6.2, for a request whose
body the filter has already rewritten (`rhi_gateway.body.Forwarded`).

**Where it goes.** One internal load balancer reaches every pool. Its listener chooses the pool's
target group by the header `X-Rhi-Pool`, whose value is the pool's name. The URL is the address
the proxy is given and the table entry's own upstream path (`rhi_gateway.routes.upstream_url`):
nothing of the client's path, query, headers or body takes part in it.

**What it sends.** The forwarded body, which the filter wrote, and headers the proxy writes itself.
None of the client's headers is copied, so the student's key (in `Authorization` or `x-api-key`)
never reaches the engine and an `X-Rhi-Pool` the client sent is dropped:

- `Authorization: Bearer` with the pool's own bearer secret (section 4.1, "Reachability");
- `X-Rhi-Pool` with the name of the pool the body filter chose;
- `Content-Type: application/json`, and `Accept-Encoding: identity`, so that the bytes that come
  back are the stream itself and not a compression of it, for the usage parser and for the client;
- `Cookie` with the load balancer's cookie the student's record holds for the pool, which keeps
  the student on one instance (section 5.5; `rhi_gateway.affinity`), and no `Cookie` header when
  the record holds none. A cookie the client sent is not copied, like the rest of its headers,
  and the upstream client, which every student's requests share, keeps no cookie of its own: the
  proxy sets its jar to accept none, so an answer's cookie is never sent with another request.

The cookie the load balancer's answer sets is read from its headers as soon as they arrive, for
the request's settlement to store, whatever the answer's status. It never reaches the student:
the client's answer is written here, and none of the upstream's headers is copied into it, so it
carries no `Set-Cookie`.

**What comes back.** A stream (an answer of type `text/event-stream`) goes to the client as each
chunk arrives. Each chunk is also fed to the usage parser of the request's wire format (section
6.2, step 6): Messages and Responses read beside the stream; Chat Completions forwards what its
parser returns, which hides the usage chunk the gateway asked for from a client that did not. On
its way to the client the stream passes one more reader, which keeps the engine's own errors out
of it: of an event's first line, not yet ended, only its field name (`event:`, `data:`) is
forwarded at once, and its value once the line is whole. An event with a name is judged first by its name line, which is
forwarded once whole unless it names an error; the rest of the event, and the whole of an event
without a name (as Chat Completions sends them), is held until the event ends and judged with its
data, however many lines carry it. An event that is an error (named `error` or `response.failed`,
or whose data is a JSON object whose `type` is `error` or `response.failed` or whose `error` is
set) is never forwarded: the stream ends there (below).

An answer that is not a stream is read whole before any of it is sent. It is sent only if it is a
whole answer of its format that reports its usage, read by the format's `read_answer` (a token
count's from its own shape, `{"input_tokens": n}`), and does not report a failure (an `error` that
is set, a `type` of `error`, a `status` of `failed`).

**How a request ends** (section 6.2, steps 3, 7 and 8). Each forwarded request is settled exactly
once, by the `on_end` it was given, with one `Observed`: the tokens observed and whether the answer
ended cleanly. A clean end is one whose answer was read whole and reported its usage (a stream: the
format's own clean end). The settlement frees the request's concurrency slot and charges nothing
(`rhi_gateway.budget`); the tokens are logged (`rhi_gateway.telemetry`). The tokens observed are
what the bytes received report, the bytes of an answer that broke off included. The other
ends:

- **the engine fails**: it cannot be reached, answers with a status that is not a success, breaks
  off, sends an error, sends an answer that is not whole, or ends a stream without the format's
  clean end. Before anything was sent, the client is answered 502 with the cause `engine_error`;
  on a stream that has started, the stream ends with the error event of its wire format for that
  cause, and nothing follows it. None of the engine's own answer, its body, its headers or its
  error events, is sent in either case;
- **the time limit**: a request may run `longest_request_seconds` at most, 1,800 by default
  (section 17), counted from the moment it is forwarded. Every wait on the engine, and every
  write of a stream to the client, races that deadline, and a deadline reached wins over an
  answer or a write that is ready with it. A request that reaches it is ended with the cause
  `time_limit`: 504 if nothing was sent, the error event of its wire format on a stream. A write
  to the client under way at the limit is given up and counted as not sent: the engine's answer is
  closed and the request settled, and the client's answer is then written for a client that still
  takes it, 504 if the stream's start was given up, else the error event after what was sent. An
  answer whose clean end is in a write given up did not end cleanly;
- **the client leaves**: while the gateway waits for the engine's headers or for an answer that is
  not a stream, the client's disconnect is listened for and ends the wait; once a stream has
  started, Starlette's own listener does. The engine's answer is closed and nothing more is sent.

A stream is settled as soon as its end is known, before its last bytes are written to the client,
so that a client slow to read them holds no reservation. The error answers and events are those of
`rhi_gateway.errors`, which reads nothing but the cause.
A stream cut in the middle of an event has that event closed before the error event, so that the
error is read as an event of its own; where what the client has of the open event is the start of
the error event (`event: `, `data: `), the error event is completed from there instead.

**Time** (section 6.2, step 7). Every timeout of the upstream client is set here, because
httpx's default, 5 seconds for each, would cut a stream that is quiet while the engine computes:
10 seconds to connect, 60 to write a request, 10 to obtain a pooled connection, and 1,200 to read,
the idle timeout both load balancers run (section 5.3), so that only the request's own time limit
ends a quiet stream. An idle upstream connection is closed after 5 seconds, one of the guards that
let a pool's instance leave service quickly (section 5.1). The time limit is waited for with the
`sleep` the proxy is given, `asyncio.sleep` unless a test gives its own clock.

**Token times** (section 7.2). The speed a student got is counted from the first token sent to the
last, so `forward` takes a `token_sent`, which the proxy calls each time a write to the
client that completed an event carrying output has returned (`carries_output`). Output is text,
reasoning or a tool call's arguments, as in the engine's output tokens, and an event carries some
when the piece it adds is not empty: a `content_block_delta` of Messages with text, thinking or a
tool's input, a `.delta` event of Responses, a Chat Completions chunk whose delta holds text or a
tool call's arguments. An empty piece is no token, and neither is what an engine sends beside its
output: a thinking block's signature, a tool call's name, the role. It is not called for a write
given up at the time limit, or for an answer that is not a stream. The first and
the last call of a request are its two times; whoever is called reads the clock
(`rhi_gateway.experience`).

The model reasons before it answers (section 4.2), and a student sees an answer begin at the first
token that is not reasoning. So each call also says whether what was just written carries answer
text or a tool call (`output_of`), as each format's reference marks its events:

- Messages: a `text_delta` or an `input_json_delta` is the answer, a `thinking_delta` reasoning;
- Responses: the reference names its reasoning events `response.reasoning_text.delta` and
  `response.reasoning_summary_text.delta`. A `.delta` event whose name begins `response.reasoning`
  is reasoning, and any other the answer: its text, a refusal, a tool call's arguments or input;
- Chat Completions: the reference gives a delta `content`, `refusal`, `tool_calls` and the
  deprecated `function_call`, which the gateway still accepts in a request, and no reasoning
  field. So a delta with one of the first two, or with the arguments of a tool call or of a
  function call, is the answer, and a delta that carries output in any other field, the engine's
  own, is reasoning.

Nothing here reads configuration or the environment: the address, the secrets and the limit are
given.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import Any, Protocol

import httpx
from starlette.responses import Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from rhi_common.causes import Cause
from rhi_gateway import routes, telemetry
from rhi_gateway.affinity import Affinity
from rhi_gateway.body import Forwarded
from rhi_gateway.errors import WireFormat, render_json, render_stream_event
from rhi_gateway.formats.chat import ChatForwarded
from rhi_gateway.routes import Route
from rhi_gateway.sse import SseEvent, SseReader
from rhi_gateway.usage import chat, messages, responses

# Seconds (section 6.2, step 7). Each is set: none is left to the library's default.
CONNECT_SECONDS = 10.0
WRITE_SECONDS = 60.0
READ_SECONDS = 1200.0
POOL_SECONDS = 10.0
KEEPALIVE_EXPIRY_SECONDS = 5.0

# The longest a request may run, in seconds (sections 6.2, step 7, and 17). A lease outlasts it.
LONGEST_REQUEST_SECONDS = 1800

UPSTREAM_TIMEOUT = httpx.Timeout(connect=CONNECT_SECONDS, read=READ_SECONDS, write=WRITE_SECONDS, pool=POOL_SECONDS)
UPSTREAM_LIMITS = httpx.Limits(keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS)

# The header the internal load balancer's listener chooses a pool's target group by.
POOL_HEADER = "X-Rhi-Pool"
STREAM_TYPE = "text/event-stream"
_TOKEN_COUNT_FIELD = "input_tokens"

# The names of the events that report an engine's failure, in the three formats.
FAILURE_EVENTS = frozenset({"error", "response.failed"})

# The status answered to a client that left before its answer: never read, it is what a log shows.
CLIENT_LEFT_STATUS = 499


def upstream_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The gateway's client of the engines, with every timeout and the idle expiry set.

    It reads no proxy setting from the environment and follows no redirect: a call goes to the
    address it was made for or nowhere. `transport` replaces the network, for tests.
    """
    return httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,
        limits=UPSTREAM_LIMITS,
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    )


@dataclass(frozen=True)
class Observed:
    """What an answer reported: the tokens, the input whole, and whether the answer ended cleanly.

    `cached_input_tokens` is the part of the input the answer reported read from the cache: already
    in `input_tokens`, and 0 when the answer reported none.
    """

    input_tokens: int
    output_tokens: int
    ended_cleanly: bool
    cached_input_tokens: int = 0

    def unclean(self) -> Observed:
        """The same tokens, from an answer that did not end cleanly."""
        return dataclasses.replace(self, ended_cleanly=False)


class _Meter(Protocol):
    """Reads one answer as it passes: what to forward of each chunk, and what was observed."""

    def feed(self, chunk: bytes) -> bytes: ...

    def observed(self) -> Observed: ...


class _MessagesMeter:
    def __init__(self) -> None:
        self._parser = messages.MessagesStreamUsage()

    def feed(self, chunk: bytes) -> bytes:
        self._parser.feed(chunk)
        return chunk

    def observed(self) -> Observed:
        seen = self._parser.observed
        return Observed(seen.input_tokens, seen.output_tokens, seen.ended_cleanly, seen.cached_input_tokens)


class _ResponsesMeter:
    def __init__(self) -> None:
        self._parser = responses.ResponsesStreamUsage()

    def feed(self, chunk: bytes) -> bytes:
        self._parser.feed(chunk)
        return chunk

    def observed(self) -> Observed:
        seen = self._parser.observed
        if seen is None:
            return Observed(0, 0, ended_cleanly=False)
        return Observed(seen.input_tokens, seen.output_tokens, self._parser.ended_cleanly, seen.cached_input_tokens)


class _ChatMeter:
    def __init__(self, *, client_asked_for_usage: bool) -> None:
        self._parser = chat.ChatStreamUsage(client_asked_for_usage=client_asked_for_usage)

    def feed(self, chunk: bytes) -> bytes:
        return self._parser.feed(chunk)

    def observed(self) -> Observed:
        seen = self._parser.observed
        return Observed(seen.input_tokens, seen.output_tokens, seen.ended_cleanly, seen.cached_input_tokens)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _json_object(body: bytes | str) -> dict[str, object]:
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _reports_failure(answer: Mapping[str, object]) -> bool:
    """Whether an answer, or an event's data, is the engine's report of a failure; any field may be of any type."""
    kind = answer.get("type")
    return (
        answer.get("error") is not None
        or (isinstance(kind, str) and kind in FAILURE_EVENTS)
        or answer.get("status") == "failed"
    )


def read_token_count(body: bytes) -> Observed:
    """The usage of a token count's answer, `{"input_tokens": n}`: the input counted, and no output."""
    counted = _count(_json_object(body).get(_TOKEN_COUNT_FIELD))
    return Observed(counted or 0, 0, ended_cleanly=counted is not None)


def read_whole_answer(route: Route, body: bytes) -> Observed:
    """The usage of an answer that is not a stream; clean only for a whole answer that reports no failure."""
    if route == routes.COUNT_TOKENS:
        seen = read_token_count(body)
    elif route.wire_format is WireFormat.RESPONSES:
        usage = responses.read_answer(body)
        seen = (
            Observed(0, 0, False)
            if usage is None
            else Observed(usage.input_tokens, usage.output_tokens, True, usage.cached_input_tokens)
        )
    else:
        read = messages.read_answer if route.wire_format is WireFormat.MESSAGES else chat.read_answer
        reported = read(body)
        seen = Observed(
            reported.input_tokens, reported.output_tokens, reported.ended_cleanly, reported.cached_input_tokens
        )
    return seen.unclean() if _reports_failure(_json_object(body)) else seen


def _stream_meter(route: Route, forwarded: Forwarded) -> _Meter:
    """The usage parser of a stream in the route's wire format."""
    if route.wire_format is WireFormat.MESSAGES:
        return _MessagesMeter()
    if route.wire_format is WireFormat.RESPONSES:
        return _ResponsesMeter()
    asked = isinstance(forwarded, ChatForwarded) and forwarded.client_asked_for_usage
    return _ChatMeter(client_asked_for_usage=asked)


def _is_failure(event: SseEvent) -> bool:
    if event.comment:
        return False
    return event.name in FAILURE_EVENTS or _reports_failure(_json_object(event.data))


class Output(enum.Enum):
    """What an event that carries output carries: the model's reasoning, or its answer (text or a tool call)."""

    REASONING = "reasoning"
    ANSWER = "answer"


# What `forward` calls once output has been written to the client: with whether some of it is the answer.
TokenSent = Callable[[bool], None]

# The events that carry output. Messages names one kind of event, and the field of each kind of piece: a
# thinking block's signature is a `signature_delta`, which is not output. Responses ends the name of
# each of its kinds alike, and the piece is the event's `delta`.
MESSAGES_OUTPUT_EVENT = "content_block_delta"
MESSAGES_OUTPUT_FIELDS = {"text_delta": "text", "thinking_delta": "thinking", "input_json_delta": "partial_json"}
RESPONSES_OUTPUT_SUFFIX = ".delta"
# Which of them carry reasoning: one kind of piece of Messages, the events of Responses named so. Chat
# Completions has no reasoning field: its answer is in these fields of a delta, in its tool calls and
# in the deprecated function call, the one call a delta carried before it carried a list of them.
MESSAGES_REASONING_PIECE = "thinking_delta"
RESPONSES_REASONING_PREFIX = "response.reasoning"
CHAT_ANSWER_FIELDS = frozenset({"content", "refusal"})
CHAT_TOOL_CALLS = "tool_calls"
CHAT_FUNCTION_CALL = "function_call"


def _is_text(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _has_arguments(function: object) -> bool:
    """Whether a function, as a delta gives it, adds a piece of its arguments: its name alone is not a token."""
    return isinstance(function, dict) and _is_text(function.get("arguments"))


def _chat_delta_output(delta: Mapping[str, object]) -> Output | None:
    """What a chunk's delta adds: answer text or a piece of a call's arguments, else any other text, as reasoning."""
    found = None
    for name, value in delta.items():
        if name == CHAT_TOOL_CALLS:
            # A call's first chunk names it, with no arguments yet: that is not a token.
            calls = value if isinstance(value, list) else []
            if any(isinstance(call, dict) and _has_arguments(call.get("function")) for call in calls):
                return Output.ANSWER
        elif name == CHAT_FUNCTION_CALL:
            if _has_arguments(value):
                return Output.ANSWER
        elif name != "role" and _is_text(value):
            if name in CHAT_ANSWER_FIELDS:
                return Output.ANSWER
            found = Output.REASONING
    return found


def output_of(wire_format: WireFormat, event: SseEvent) -> Output | None:
    """What an event of a stream carries of the output, if any: a piece, not empty, of reasoning or of the answer.

    The answer is its text or a tool call's arguments. An event that carries both is the answer's.
    """
    if event.comment:
        return None
    data = _json_object(event.data)
    if wire_format is WireFormat.CHAT_COMPLETIONS:
        choices = data.get("choices")
        found = {
            _chat_delta_output(delta)
            for choice in (choices if isinstance(choices, list) else [])
            if isinstance(choice, dict) and isinstance(delta := choice.get("delta"), dict)
        }
        return Output.ANSWER if Output.ANSWER in found else Output.REASONING if Output.REASONING in found else None
    kind = event.name or data.get("type")
    if not isinstance(kind, str):
        return None
    if wire_format is WireFormat.RESPONSES:
        if not kind.endswith(RESPONSES_OUTPUT_SUFFIX) or not _is_text(data.get("delta")):
            return None
        return Output.REASONING if kind.startswith(RESPONSES_REASONING_PREFIX) else Output.ANSWER
    delta = data.get("delta")
    if kind != MESSAGES_OUTPUT_EVENT or not isinstance(delta, dict):
        return None
    piece = delta.get("type")
    field = MESSAGES_OUTPUT_FIELDS.get(piece) if isinstance(piece, str) else None
    if field is None or not _is_text(delta.get(field)):
        return None
    return Output.REASONING if piece == MESSAGES_REASONING_PIECE else Output.ANSWER


def carries_output(wire_format: WireFormat, event: SseEvent) -> bool:
    """Whether an event of a stream carries output: a piece, not empty, of text, reasoning or a tool call's arguments."""
    return output_of(wire_format, event) is not None


def _event_name(line: bytes) -> str:
    """The name an `event:` line gives, as the SSE reader reads it: one space after the colon is not part of it."""
    value = line[len(b"event:") :].rstrip(b"\r\n")
    return value.removeprefix(b" ").decode("utf-8", "replace")


class _ErrorGate:
    """Forwards a stream's bytes in order and stops at the first event that is an engine's error.

    Of the first line of an event, not yet ended, the field name is forwarded at once and the value
    waits for the end of the line. An event's name line is judged by the name it gives, and
    forwarded if that is not an error's; every other line of an event is held until the event ends,
    and judged with the event's data.
    """

    def __init__(self) -> None:
        self._reader = SseReader()
        self._line = bytearray()  # The line not yet ended, as received.
        self._line_sent = 0  # How much of it the client has.
        self._open = bytearray()  # What the client has of the event not yet ended.
        self._held = bytearray()  # Whole lines of that event the client does not have yet.
        self._named = False  # Whether that event's name line was read, forwarded, and is not an error's.
        self.failed = False  # An error event was read: nothing more is forwarded.
        self.failed_event_ended = False  # And the stream has reached that event's end.

    def feed(self, chunk: bytes) -> bytes:
        """Take the next bytes for the client and return those that may be sent now."""
        forward = bytearray()
        *ended, rest = chunk.split(b"\n")
        for end in ended:
            self._line += end + b"\n"
            line, unsent = bytes(self._line), bytes(self._line[self._line_sent :])
            self._line.clear()
            self._line_sent = 0
            self._take(line, unsent, forward)
            if self.failed_event_ended:
                return bytes(forward)
        self._line += rest
        if not self.failed and not self._held and not self._named:
            colon = self._line.find(b":")
            if colon >= 0:
                field_end = colon + 1 + (self._line[colon + 1 : colon + 2] == b" ")
                if field_end > self._line_sent:
                    self._send(bytes(self._line[self._line_sent : field_end]), forward)
                    self._line_sent = field_end
        return bytes(forward)

    def _take(self, line: bytes, unsent: bytes, forward: bytearray) -> None:
        """Read one whole line, of which the client has all but `unsent`."""
        blank = line in (b"\n", b"\r\n")
        failure = any(_is_failure(event) for event in self._reader.feed(line))
        if self.failed:
            self.failed_event_ended = blank
            return
        first_name = line.startswith(b"event:") and not self._held and not self._named
        if first_name and _event_name(line) in FAILURE_EVENTS:
            failure = True  # Judged by its name, before any of its data is read.
        if failure:
            self.failed = True
            self.failed_event_ended = blank
            self._held.clear()
            return
        if blank:
            # The event ended and is not an error: whatever of it was held goes now, then a new one starts.
            self._send(bytes(self._held) + unsent, forward)
            self._held.clear()
            self._open.clear()
            self._named = False
        elif (
            line.startswith(b":") and not self._held and not self._named and len(self._open) == len(line) - len(unsent)
        ):
            # A comment between events, a keep-alive: sent as soon as it is whole, and part of no event.
            self._send(unsent, forward)
            self._open.clear()
        elif first_name:
            self._named = True
            self._send(unsent, forward)
        else:
            self._held += unsent

    def _send(self, data: bytes, forward: bytearray) -> None:
        forward += data
        self._open += data

    @property
    def client_has(self) -> bytes:
        """What the client has of the event not yet ended."""
        return bytes(self._open)

    def ending(self, event: bytes, client_has: bytes | None = None) -> bytes:
        """What ends the stream with `event`, after what the client has of the open event (as forwarded, by default)."""
        open_event = self.client_has if client_has is None else client_has
        if event.startswith(open_event):
            return event[len(open_event) :]
        return (b"\n" if open_event.endswith(b"\n") else b"\n\n") + event


class TimeLimitReached(Exception):
    """The request has reached the longest duration it was given."""


class ClientLeft(Exception):
    """The client disconnected while the gateway waited for the engine."""


async def _next_chunk(chunks: AsyncIterator[bytes]) -> bytes | None:
    """The next chunk, or `None` once there is none: a task may not end in `StopAsyncIteration`."""
    try:
        return await anext(chunks)
    except StopAsyncIteration:
        return None


async def _until_disconnect(receive: Receive) -> None:
    """Return once the client has disconnected; the request's body was read already."""
    while (await receive())["type"] != "http.disconnect":
        pass


class _Ends:
    """What may end one request while it waits on the engine: its time limit, and the client leaving."""

    def __init__(self, sleep: Callable[[float], Awaitable[None]], seconds: float, receive: Receive | None) -> None:
        self._timer = asyncio.ensure_future(sleep(seconds))
        self._left = None if receive is None else asyncio.ensure_future(_until_disconnect(receive))
        self._step: asyncio.Future[Any] | None = None  # The wait on the engine, or the client, under way.
        self.closed = False  # The request has ended: nothing is raced any more.

    def _check(self) -> None:
        if self._timer.done() and not self._timer.cancelled():
            raise TimeLimitReached
        if self._left is not None and self._left.done():
            raise ClientLeft

    async def race[T](self, waited: Coroutine[Any, Any, T]) -> T:
        """What `waited` gives, unless the time limit or the client's leaving comes first.

        A limit already reached wins over an answer already waiting: `waited` is not started. A
        limit reached while `waited` ran wins over what `waited` gave, should both be ready together.
        """
        try:
            self._check()
        except BaseException:
            waited.close()
            raise
        step = asyncio.ensure_future(waited)
        self._step = step
        rivals = [self._timer] if self._left is None else [self._timer, self._left]
        try:
            await asyncio.wait((step, *rivals), return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancelled as well when the request is (the client left): `close` waits for it to stop.
            if not step.done():
                step.cancel()
        try:
            self._check()
        except BaseException:
            if step.done() and not step.cancelled():
                step.exception()  # Retrieved: what the step gave is given up for the end that came with it.
            raise
        self._step = None
        return step.result()

    async def stop_listening(self) -> None:
        """Stop listening for the client, whose stream's own listener takes over; a client already gone ends it."""
        if self._left is None:
            return
        left, self._left = self._left, None
        if left.done():
            self._left = left
            self._check()
        left.cancel()
        await asyncio.wait((left,))

    async def close(self) -> None:
        """Stop the timer and the listener, and wait for a cancelled wait on the engine to stop."""
        self.closed = True
        self._timer.cancel()
        waits = [wait for wait in (self._step, self._left) if wait is not None]
        self._step = self._left = None
        for wait in waits:
            wait.cancel()
        if waits:
            await asyncio.wait(waits)
        for wait in waits:
            if not wait.cancelled():
                wait.exception()  # Retrieved: what a wait gave after it was given up on is dropped.


class _Settlement:
    """The `on_end` of one request, called once at most however many of its ends race to call it."""

    def __init__(self, on_end: Callable[[Observed], Awaitable[None]]) -> None:
        self._on_end = on_end
        self._settled = False

    async def __call__(self, observed: Observed) -> None:
        if self._settled:
            return
        self._settled = True
        await self._on_end(observed)


class _Stream:
    """A streamed answer, open: its chunks for the client, the error event that ends a broken one, its usage."""

    def __init__(
        self,
        response: httpx.Response,
        meter: _Meter,
        ends: _Ends,
        wire_format: WireFormat,
        settle: _Settlement,
        *,
        token_sent: TokenSent | None = None,
    ) -> None:
        self._response = response
        # Decoded, should the engine compress what it was asked to send as it is: the client is
        # given no `Content-Encoding`, and the parser reads text.
        self._upstream = response.aiter_bytes()
        self._meter = meter
        self._gate = _ErrorGate()
        self.ends = ends
        self._wire_format = wire_format
        self._settle = settle
        self._clean = False
        self._abandoned = False  # The last write of a chunk to the client was given up at the time limit.
        self._token_sent = token_sent
        self._sent = SseReader()  # Reads what is written to the client, for the events that carry output.

    def abandon_write(self) -> None:
        """The write of the chunk last given was given up: the client does not have it."""
        self._abandoned = True

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def wire_format(self) -> WireFormat:
        return self._wire_format

    @property
    def content_type(self) -> str | None:
        return self._response.headers.get("content-type")

    async def chunks(self) -> AsyncGenerator[bytes]:
        """The bytes for the client, each as soon as the upstream chunk that carries it has arrived."""
        upstream = self._upstream
        cause = Cause.ENGINE_ERROR
        client_has: bytes | None = None  # What the client has of the open event, if not all that was forwarded.
        try:
            while (chunk := await self.ends.race(_next_chunk(upstream))) is not None:
                before = self._gate.client_has
                forward = self._gate.feed(self._meter.feed(chunk))
                if forward:
                    carried = (
                        set()
                        if self._token_sent is None
                        else {output_of(self._wire_format, event) for event in self._sent.feed(forward)} - {None}
                    )
                    yield forward
                    if self._abandoned:
                        client_has = before
                        raise TimeLimitReached
                    if carried and self._token_sent is not None:
                        # The write has returned: the client has the token, and the answer's first if it is one.
                        self._token_sent(Output.ANSWER in carried)
                if self._gate.failed_event_ended:
                    break
        except TimeLimitReached:
            cause = Cause.TIME_LIMIT
        except httpx.RequestError:
            pass  # The upstream broke off: the answer is cut where it stands.
        # The format's own clean end was read, and sent: the answer is whole, whatever came after it.
        self._clean = not self._abandoned and not self._gate.failed and self._meter.observed().ended_cleanly
        # Settled before the last bytes are written, which a client may be slow to take.
        await _to_the_end(self.end())
        if self._clean:
            return
        # An error, ended early, broken off or out of time: the gateway's error event, and nothing after it.
        if self._gate.failed:
            cause = Cause.ENGINE_ERROR
        yield self._gate.ending(_error_event(cause, self._wire_format), client_has)

    @property
    def observed(self) -> Observed:
        """The tokens observed so far; clean only for a stream that reached its format's clean end."""
        return dataclasses.replace(self._meter.observed(), ended_cleanly=self._clean)

    async def end(self) -> None:
        """Close the engine's answer and settle the request with what was observed; once, however often called."""
        try:
            await self.ends.close()
            await self._response.aclose()
        finally:
            await self._settle(self.observed)


def _error_event(cause: Cause, wire_format: WireFormat) -> bytes:
    """The event that ends a stream with `cause`, noted on the request's log record."""
    telemetry.note_cause(cause)
    return render_stream_event(cause, wire_format)


def _is_stream(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == STREAM_TYPE


def _error_answer(cause: Cause, wire_format: WireFormat) -> Response:
    telemetry.note_cause(cause)
    answer = render_json(cause, wire_format)
    return Response(content=answer.body, status_code=answer.status, headers=answer.headers)


class Proxy:
    """Sends filtered requests to the pools behind one address, each with its pool's secret.

    `base_url` is the internal load balancer's address and `secrets` the bearer secret of each
    pool, by the pool's name: both are configuration, never anything a client sent.
    `longest_request_seconds` is the longest a request may run, and `sleep` what waits for it.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        secrets: Mapping[str, str],
        *,
        longest_request_seconds: int = LONGEST_REQUEST_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if longest_request_seconds <= 0:
            raise ValueError("the longest a request may run must be a positive number of seconds")
        self._client = client
        # Every student's requests share this client: its jar takes no cookie from an answer and
        # gives none to a request, so the only cookie sent is the one the request's student holds.
        client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=()))
        self._base_url = base_url
        self._secrets = dict(secrets)
        self._longest = longest_request_seconds
        self._sleep = sleep

    @property
    def longest_request_seconds(self) -> int:
        return self._longest

    async def _send(self, route: Route, forwarded: Forwarded, affinity: Affinity | None = None) -> httpx.Response:
        """Send the request and return the answer as soon as its headers have arrived.

        The request carries the cookie of `affinity`, which is told the cookie the answer sets.
        """
        secret = self._secrets.get(forwarded.pool.name)
        if not secret:
            raise LookupError("the pool has no bearer secret")
        request = self._client.build_request(
            "POST",
            routes.upstream_url(self._base_url, route),
            content=forwarded.body,
            headers={
                "Authorization": "Bearer " + secret,
                POOL_HEADER: forwarded.pool.name,
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
                **({} if affinity is None else affinity.headers()),
            },
        )
        response = await self._client.send(request, stream=True)
        if affinity is not None:
            affinity.answered(response.headers.get_list("set-cookie"))
        return response

    async def forward(
        self,
        route: Route,
        forwarded: Forwarded,
        on_end: Callable[[Observed], Awaitable[None]],
        *,
        receive: Receive | None = None,
        token_sent: TokenSent | None = None,
        affinity: Affinity | None = None,
    ) -> Response:
        """Forward the request and return the client's answer; `on_end` is called exactly once, at its end.

        `receive` is the client's, whose body has been read: it is listened to for the client's
        disconnect until a stream starts. A stream's end comes once the client's answer has been
        sent or abandoned; every other end comes before this returns. `token_sent` is called each
        time an event of a stream that carries output has been written to the client, with whether
        the output written is the answer's (text or a tool call) and not only reasoning. `affinity`
        holds the cookie to send to the load balancer, and is told the one its answer sets before
        `on_end` is called.
        """
        settle = _Settlement(on_end)
        wire_format = route.wire_format or WireFormat.MESSAGES
        ends = _Ends(self._sleep, self._longest, receive)
        response: httpx.Response | None = None
        received = bytearray()  # The body of an answer that is not a stream, as far as it came.
        whole = False
        streaming = False
        try:
            try:
                response = await ends.race(self._send(route, forwarded, affinity))
                if not response.is_success:
                    # The engine failed: none of its answer reaches the client.
                    return _error_answer(Cause.ENGINE_ERROR, wire_format)
                content_type = response.headers.get("content-type", "")
                if _is_stream(content_type):
                    await ends.stop_listening()
                    streaming = True
                    meter = _stream_meter(route, forwarded)
                    return ProxiedResponse(_Stream(response, meter, ends, wire_format, settle, token_sent=token_sent))
                chunks = response.aiter_bytes()
                while (chunk := await ends.race(_next_chunk(chunks))) is not None:
                    received += chunk
                if not read_whole_answer(route, bytes(received)).ended_cleanly:
                    # Cut short, or the engine's report of a failure: none of it reaches the client.
                    return _error_answer(Cause.ENGINE_ERROR, wire_format)
                whole = True
                return Response(
                    content=bytes(received), status_code=response.status_code, media_type=content_type or None
                )
            except TimeLimitReached:
                return _error_answer(Cause.TIME_LIMIT, wire_format)
            except ClientLeft:
                return Response(status_code=CLIENT_LEFT_STATUS)
            except httpx.RequestError:
                # Not reached, or broken off before its answer was whole.
                return _error_answer(Cause.ENGINE_ERROR, wire_format)
        finally:
            if not streaming:
                seen = _observed_of(route, bytes(received))
                await _to_the_end(_close_and_settle(ends, response, settle, seen if whole else seen.unclean()))


def _observed_of(route: Route, body: bytes) -> Observed:
    """What an answer that is not a stream reported; nothing, should reading it fail, so that it is settled still."""
    try:
        return read_whole_answer(route, body)
    except Exception:  # noqa: BLE001 - the settlement must be made whatever the engine sent; it then observes nothing.
        return Observed(0, 0, ended_cleanly=False)


async def _close_and_settle(
    ends: _Ends, response: httpx.Response | None, settle: _Settlement, observed: Observed
) -> None:
    """End a request whose answer was not a stream, with what its body reported."""
    try:
        await ends.close()
        if response is not None:
            await response.aclose()
    finally:
        await settle(observed)


async def _to_the_end(work: Awaitable[None]) -> None:
    """Run `work` to its end even if the caller is cancelled meanwhile, so that no settlement is lost."""
    ending = asyncio.ensure_future(work)
    _ENDING.add(ending)
    ending.add_done_callback(_ENDING.discard)
    await asyncio.shield(ending)


class _LimitedSend:
    """The client's `send`, each write racing the request's time limit until the request has ended."""

    def __init__(self, send: Send, ends: _Ends) -> None:
        self._send = send
        self._ends = ends
        self.started = False  # The answer's start has reached the client.

    async def _write(self, message: Message) -> None:
        await self._send(message)
        if message["type"] == "http.response.start":
            self.started = True

    async def __call__(self, message: Message) -> None:
        if self._ends.closed:
            await self._write(message)
        else:
            await self._ends.race(self._write(message))


class ProxiedResponse(StreamingResponse):
    """The client's stream: the upstream's chunks, then the settlement with what was observed, exactly once.

    The settlement is made however the stream ends, the client gone included, and is shielded from
    the cancellation that a client's disconnect brings, so that it is not lost.
    """

    def __init__(self, stream: _Stream) -> None:
        self._chunks = stream.chunks()
        super().__init__(self._chunks, status_code=stream.status_code, media_type=stream.content_type)
        self._stream = stream

    async def stream_response(self, send: Send) -> None:
        limited = _LimitedSend(send, self._stream.ends)
        try:
            await super().stream_response(limited)
        except TimeLimitReached:
            # The limit came during a write to the client, which is given up: the client does not have it.
            if not limited.started:
                # Nothing was sent: the request is settled, then answered 504, should the client still take it.
                await _to_the_end(self._end())
                answer = _error_answer(Cause.TIME_LIMIT, self._stream.wire_format)
                await send({"type": "http.response.start", "status": answer.status_code, "headers": answer.raw_headers})
                await send({"type": "http.response.body", "body": answer.body, "more_body": False})
                return
            # The stream resumes, reaches its limit, and is settled; its error event is then written.
            self._stream.abandon_write()
            async for chunk in self._chunks:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _to_the_end(self._end())

    async def _end(self) -> None:
        try:
            await self._chunks.aclose()
        finally:
            await self._stream.end()


# The ends under way, held until each is done: a task the loop alone refers to may be collected.
_ENDING: set[asyncio.Future[None]] = set()
