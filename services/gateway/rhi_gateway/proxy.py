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
  back are the stream itself and not a compression of it, for the usage parser and for the client.

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
format's own clean end). The ledger charges a clean end the usage reported, and any other end the
tokens observed, never less than the input estimate (`rhi_gateway.budget`). The tokens observed
are what the bytes received report, the bytes of an answer that broke off included. The other
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
  to the client under way at the limit is given up, the engine's answer is closed and the request
  settled, and the error event is then written for a client that still takes it;
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

Nothing here reads configuration or the environment: the address, the secrets and the limit are
given.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from starlette.responses import Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from rhi_common.causes import Cause
from rhi_gateway import routes
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
    """What an answer reported: the tokens, the input whole, and whether the answer ended cleanly."""

    input_tokens: int
    output_tokens: int
    ended_cleanly: bool

    def unclean(self) -> Observed:
        """The same tokens, from an answer that did not end cleanly."""
        return Observed(self.input_tokens, self.output_tokens, ended_cleanly=False)


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
        return Observed(seen.input_tokens, seen.output_tokens, seen.ended_cleanly)


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
        return Observed(seen.input_tokens, seen.output_tokens, self._parser.ended_cleanly)


class _ChatMeter:
    def __init__(self, *, client_asked_for_usage: bool) -> None:
        self._parser = chat.ChatStreamUsage(client_asked_for_usage=client_asked_for_usage)

    def feed(self, chunk: bytes) -> bytes:
        return self._parser.feed(chunk)

    def observed(self) -> Observed:
        seen = self._parser.observed
        return Observed(seen.input_tokens, seen.output_tokens, seen.ended_cleanly)


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
        seen = Observed(0, 0, False) if usage is None else Observed(usage.input_tokens, usage.output_tokens, True)
    else:
        read = messages.read_answer if route.wire_format is WireFormat.MESSAGES else chat.read_answer
        reported = read(body)
        seen = Observed(reported.input_tokens, reported.output_tokens, reported.ended_cleanly)
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

    def ending(self, event: bytes) -> bytes:
        """What ends the stream with `event`, after what the client already has of the open event."""
        if event.startswith(self._open):
            return event[len(self._open) :]
        return (b"\n" if self._open.endswith(b"\n") else b"\n\n") + event


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
        self, response: httpx.Response, meter: _Meter, ends: _Ends, wire_format: WireFormat, settle: _Settlement
    ) -> None:
        self._response = response
        self._meter = meter
        self._gate = _ErrorGate()
        self.ends = ends
        self._wire_format = wire_format
        self._settle = settle
        self._clean = False

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def content_type(self) -> str | None:
        return self._response.headers.get("content-type")

    async def chunks(self) -> AsyncGenerator[bytes]:
        """The bytes for the client, each as soon as the upstream chunk that carries it has arrived."""
        # Decoded, should the engine compress what it was asked to send as it is: the client is
        # given no `Content-Encoding`, and the parser reads text.
        upstream = self._response.aiter_bytes()
        cause = Cause.ENGINE_ERROR
        try:
            while (chunk := await self.ends.race(_next_chunk(upstream))) is not None:
                forward = self._gate.feed(self._meter.feed(chunk))
                if forward:
                    yield forward
                if self._gate.failed_event_ended:
                    break
        except TimeLimitReached:
            cause = Cause.TIME_LIMIT
        except httpx.RequestError:
            pass  # The upstream broke off: the answer is cut where it stands.
        # The format's own clean end was read: the answer is whole, whatever came after it.
        self._clean = not self._gate.failed and self._meter.observed().ended_cleanly
        # Settled before the last bytes are written, which a client may be slow to take.
        await _to_the_end(self.end())
        if self._clean:
            return
        # An error, ended early, broken off or out of time: the gateway's error event, and nothing after it.
        if self._gate.failed:
            cause = Cause.ENGINE_ERROR
        yield self._gate.ending(render_stream_event(cause, self._wire_format))

    @property
    def observed(self) -> Observed:
        """The tokens observed so far; clean only for a stream that reached its format's clean end."""
        seen = self._meter.observed()
        return Observed(seen.input_tokens, seen.output_tokens, ended_cleanly=self._clean)

    async def end(self) -> None:
        """Close the engine's answer and settle the request with what was observed; once, however often called."""
        try:
            await self.ends.close()
            await self._response.aclose()
        finally:
            await self._settle(self.observed)


def _is_stream(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() == STREAM_TYPE


def _error_answer(cause: Cause, wire_format: WireFormat) -> Response:
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
        self._base_url = base_url
        self._secrets = dict(secrets)
        self._longest = longest_request_seconds
        self._sleep = sleep

    @property
    def longest_request_seconds(self) -> int:
        return self._longest

    async def _send(self, route: Route, forwarded: Forwarded) -> httpx.Response:
        """Send the request and return the answer as soon as its headers have arrived."""
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
            },
        )
        return await self._client.send(request, stream=True)

    async def forward(
        self,
        route: Route,
        forwarded: Forwarded,
        on_end: Callable[[Observed], Awaitable[None]],
        *,
        receive: Receive | None = None,
    ) -> Response:
        """Forward the request and return the client's answer; `on_end` is called exactly once, at its end.

        `receive` is the client's, whose body has been read: it is listened to for the client's
        disconnect until a stream starts. A stream's end comes once the client's answer has been
        sent or abandoned; every other end comes before this returns.
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
                response = await ends.race(self._send(route, forwarded))
                if not response.is_success:
                    # The engine failed: none of its answer reaches the client.
                    return _error_answer(Cause.ENGINE_ERROR, wire_format)
                content_type = response.headers.get("content-type", "")
                if _is_stream(content_type):
                    await ends.stop_listening()
                    streaming = True
                    meter = _stream_meter(route, forwarded)
                    return ProxiedResponse(_Stream(response, meter, ends, wire_format, settle))
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
    except Exception:  # noqa: BLE001 - the settlement must be made whatever the engine sent; it then charges the estimate.
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
            # The limit came during a write to the client, which is given up. The stream resumes, reaches
            # its limit, and is settled; its error event is then written, should the client still take it.
            if not limited.started:
                return
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
