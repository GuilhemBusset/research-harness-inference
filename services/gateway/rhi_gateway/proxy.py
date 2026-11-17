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

**What comes back.** A stream (an answer of type `text/event-stream`) goes to the client chunk by
chunk, as each arrives, and nothing is buffered on the way. Each chunk is also fed to the usage
parser of the request's wire format (section 6.2, step 6): Messages and Responses read beside the
stream and forward the chunk as it came; Chat Completions forwards what its parser returns, which
hides the usage chunk the gateway asked for from a client that did not. An answer that is not a
stream is read whole before any of it is sent, and its usage read by the format's `read_answer`; a
token count's usage is read from its own shape, `{"input_tokens": n}`, with no output.

**How a request ends** (section 6.2, steps 3, 7 and 8). Each forwarded request is settled exactly
once, by the `on_end` it was given, with one `Observed`: the tokens observed and whether the answer
ended cleanly. A clean end is one whose answer was read whole and reported its usage (a stream: the
format's own clean end). The ledger charges a clean end the usage reported, and any other end the
tokens observed, never less than the input estimate (`rhi_gateway.budget`). The other ends:

- **the engine fails**: it cannot be reached, answers with a status that is not a success, breaks
  off, or ends a stream without the format's clean end. Before anything was sent, the client is
  answered 502 with the cause `engine_error`; on a stream that has started, the stream ends with the
  error event of its wire format for that cause, and nothing follows it. None of the engine's own
  answer, its body or its headers, is sent in either case;
- **the time limit**: a request may run `longest_request_seconds` at most, 1,800 by default
  (section 17), counted from the moment it is forwarded. Every wait on the engine races that
  deadline. A request that reaches it is ended with the cause `time_limit`: 504 if nothing was sent,
  the error event of its wire format on a stream;
- **the client leaves**: the stream is stopped and the engine's answer closed.

The error answers and events are those of `rhi_gateway.errors`, which reads nothing but the cause.
A stream cut in the middle of an event has that event closed by a blank line before the error event,
so that the error is read as an event of its own.

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
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from rhi_common.causes import Cause
from rhi_gateway import routes
from rhi_gateway.body import Forwarded
from rhi_gateway.errors import WireFormat, render_json, render_stream_event
from rhi_gateway.formats.chat import ChatForwarded
from rhi_gateway.routes import Route
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


class _Meter(Protocol):
    """Reads one answer as it passes: what to forward of each chunk, and what was observed."""

    def feed(self, chunk: bytes) -> bytes: ...

    def finish(self) -> bytes: ...

    def observed(self) -> Observed: ...


class _MessagesMeter:
    def __init__(self) -> None:
        self._parser = messages.MessagesStreamUsage()

    def feed(self, chunk: bytes) -> bytes:
        self._parser.feed(chunk)
        return chunk

    def finish(self) -> bytes:
        return b""

    def observed(self) -> Observed:
        seen = self._parser.observed
        return Observed(seen.input_tokens, seen.output_tokens, seen.ended_cleanly)


class _ResponsesMeter:
    def __init__(self) -> None:
        self._parser = responses.ResponsesStreamUsage()

    def feed(self, chunk: bytes) -> bytes:
        self._parser.feed(chunk)
        return chunk

    def finish(self) -> bytes:
        return b""

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

    def finish(self) -> bytes:
        return self._parser.finish()

    def observed(self) -> Observed:
        seen = self._parser.observed
        return Observed(seen.input_tokens, seen.output_tokens, seen.ended_cleanly)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _json_object(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def read_token_count(body: bytes) -> Observed:
    """The usage of a token count's answer, `{"input_tokens": n}`: the input counted, and no output."""
    counted = _count(_json_object(body).get(_TOKEN_COUNT_FIELD))
    return Observed(counted or 0, 0, ended_cleanly=counted is not None)


def read_whole_answer(route: Route, body: bytes) -> Observed:
    """The usage of an answer that is not a stream, from its whole body."""
    if route == routes.COUNT_TOKENS:
        return read_token_count(body)
    if route.wire_format is WireFormat.RESPONSES:
        seen = responses.read_answer(body)
        return Observed(0, 0, False) if seen is None else Observed(seen.input_tokens, seen.output_tokens, True)
    read = messages.read_answer if route.wire_format is WireFormat.MESSAGES else chat.read_answer
    usage = read(body)
    return Observed(usage.input_tokens, usage.output_tokens, usage.ended_cleanly)


def _stream_meter(route: Route, forwarded: Forwarded) -> _Meter:
    """The usage parser of a stream in the route's wire format."""
    if route.wire_format is WireFormat.MESSAGES:
        return _MessagesMeter()
    if route.wire_format is WireFormat.RESPONSES:
        return _ResponsesMeter()
    asked = isinstance(forwarded, ChatForwarded) and forwarded.client_asked_for_usage
    return _ChatMeter(client_asked_for_usage=asked)


class TimeLimitReached(Exception):
    """The request has reached the longest duration it was given."""


async def _next_chunk(chunks: AsyncIterator[bytes]) -> bytes | None:
    """The next chunk, or `None` once there is none: a task may not end in `StopAsyncIteration`."""
    try:
        return await anext(chunks)
    except StopAsyncIteration:
        return None


class _Deadline:
    """The time limit of one request: each wait on the engine is raced against it."""

    def __init__(self, sleep: Callable[[float], Awaitable[None]], seconds: float) -> None:
        self._timer = asyncio.ensure_future(sleep(seconds))
        self._step: asyncio.Future[Any] | None = None  # The wait on the engine under way.

    async def race[T](self, waited: Awaitable[T]) -> T:
        """What `waited` gives, or `TimeLimitReached` if the deadline comes first."""
        step = asyncio.ensure_future(waited)
        self._step = step
        try:
            done, _ = await asyncio.wait((step, self._timer), return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancelled as well when the request is (the client left): `close` waits for it to stop.
            if not step.done():
                step.cancel()
        if step not in done:
            raise TimeLimitReached
        self._step = None
        return step.result()

    async def close(self) -> None:
        """Stop the timer, and wait for a cancelled wait on the engine to stop before it is closed."""
        self._timer.cancel()
        step, self._step = self._step, None
        if step is not None:
            step.cancel()
            await asyncio.wait((step,))
            if not step.cancelled():
                step.exception()  # Retrieved: what a wait gave after it was given up on is dropped.


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

    def __init__(self, response: httpx.Response, meter: _Meter, deadline: _Deadline, wire_format: WireFormat) -> None:
        self._response = response
        self._meter = meter
        self._deadline = deadline
        self._wire_format = wire_format
        self._clean = False
        self._tail = b""  # The last bytes sent, to tell whether the client is at the start of an event.

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
        cause: Cause | None = None
        try:
            while (chunk := await self._deadline.race(_next_chunk(upstream))) is not None:
                forward = self._meter.feed(chunk)
                if forward:
                    yield self._sending(forward)
        except TimeLimitReached:
            cause = Cause.TIME_LIMIT
        except httpx.RequestError:
            # The upstream broke off: the answer is cut where it stands.
            cause = Cause.ENGINE_ERROR
        if cause is None:
            rest = self._meter.finish()
            if rest:
                yield self._sending(rest)
        if self._meter.observed().ended_cleanly:
            # The format's own clean end was read: the answer is whole, whatever came after it.
            self._clean = True
            return
        # Ended early, broken off or out of time: the error event, and nothing after it.
        yield self._separator() + render_stream_event(cause or Cause.ENGINE_ERROR, self._wire_format)

    def _sending(self, chunk: bytes) -> bytes:
        self._tail = (self._tail + chunk)[-2:]
        return chunk

    def _separator(self) -> bytes:
        """What closes the line and the event a cut stream left unfinished, so that the error is its own event."""
        if not self._tail or self._tail.endswith(b"\n\n"):
            return b""
        return b"\n" if self._tail.endswith(b"\n") else b"\n\n"

    @property
    def observed(self) -> Observed:
        """The tokens observed so far; clean only for a stream that reached its format's clean end."""
        seen = self._meter.observed()
        return Observed(seen.input_tokens, seen.output_tokens, ended_cleanly=self._clean)

    async def aclose(self) -> None:
        await self._deadline.close()
        await self._response.aclose()


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
        self, route: Route, forwarded: Forwarded, on_end: Callable[[Observed], Awaitable[None]]
    ) -> Response:
        """Forward the request and return the client's answer; `on_end` is called exactly once, at its end.

        A stream's end comes once the client's answer has been sent or abandoned; every other end
        comes before this returns.
        """
        settle = _Settlement(on_end)
        wire_format = route.wire_format or WireFormat.MESSAGES
        deadline = _Deadline(self._sleep, self._longest)
        response: httpx.Response | None = None
        streaming = False
        try:
            try:
                response = await deadline.race(self._send(route, forwarded))
                if not response.is_success:
                    # The engine failed: none of its answer reaches the client.
                    return _error_answer(Cause.ENGINE_ERROR, wire_format)
                content_type = response.headers.get("content-type", "")
                if not _is_stream(content_type):
                    body = await deadline.race(response.aread())
                    await settle(read_whole_answer(route, body))
                    return Response(content=body, status_code=response.status_code, media_type=content_type or None)
                streaming = True
                meter = _stream_meter(route, forwarded)
                return ProxiedResponse(_Stream(response, meter, deadline, wire_format), settle)
            except TimeLimitReached:
                return _error_answer(Cause.TIME_LIMIT, wire_format)
            except httpx.RequestError:
                # Not reached, or broken off before its answer was whole.
                return _error_answer(Cause.ENGINE_ERROR, wire_format)
        finally:
            if not streaming:
                await _to_the_end(_close_and_settle(deadline, response, settle))


async def _close_and_settle(deadline: _Deadline, response: httpx.Response | None, settle: _Settlement) -> None:
    """End a request whose answer was not a stream: nothing observed, unless it was settled already."""
    try:
        await deadline.close()
        if response is not None:
            await response.aclose()
    finally:
        await settle(Observed(0, 0, ended_cleanly=False))


async def _to_the_end(work: Awaitable[None]) -> None:
    """Run `work` to its end even if the caller is cancelled meanwhile, so that no settlement is lost."""
    ending = asyncio.ensure_future(work)
    _ENDING.add(ending)
    ending.add_done_callback(_ENDING.discard)
    await asyncio.shield(ending)


class ProxiedResponse(StreamingResponse):
    """The client's stream: the upstream's chunks, then the settlement with what was observed, exactly once.

    The settlement is made however the stream ends, the client gone included, and is shielded from
    the cancellation that a client's disconnect brings, so that it is not lost.
    """

    def __init__(self, stream: _Stream, settle: _Settlement) -> None:
        self._chunks = stream.chunks()
        super().__init__(self._chunks, status_code=stream.status_code, media_type=stream.content_type)
        self._stream = stream
        self._settle = settle

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await _to_the_end(self._end())

    async def _end(self) -> None:
        try:
            await self._chunks.aclose()
            await self._stream.aclose()
        finally:
            await self._settle(self._stream.observed)


# The ends under way, held until each is done: a task the loop alone refers to may be collected.
_ENDING: set[asyncio.Future[None]] = set()
