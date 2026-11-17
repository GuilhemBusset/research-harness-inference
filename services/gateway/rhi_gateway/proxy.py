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

**What comes back** goes to the client chunk by chunk, as each arrives, and nothing is buffered on
the way. Each chunk is also fed to the usage parser of the request's wire format (section 6.2,
step 6): Messages and Responses read beside the stream and forward the chunk as it came; Chat
Completions forwards what its parser returns, which hides the usage chunk the gateway asked for
from a client that did not. An answer that is not a stream (its type is not `text/event-stream`)
is forwarded the same way and read whole once it has ended, by the format's `read_answer`; a token
count is read from its own shape, `{"input_tokens": n}`, with no output. What the caller is given
at the end is one `Observed`: the tokens observed and whether the answer ended cleanly.

**Time** (section 6.2, step 7). Every timeout of the upstream client is set here, because
httpx's default, 5 seconds for each, would cut a stream that is quiet while the engine computes:
10 seconds to connect, 60 to write a request, 10 to obtain a pooled connection, and 1,200 to read,
the idle timeout both load balancers run (section 5.3), so that only the request's own time limit
ends a quiet stream. An idle upstream connection is closed after 5 seconds, one of the guards that
let a pool's instance leave service quickly (section 5.1).

This module is the path of an answer that ends well. A cut stream, an upstream error, a timeout
and the 30-minute limit each end the request otherwise; how each is told to the client is not
written here. Nothing here reads configuration or the environment: the address and the secrets
are given.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from rhi_gateway import routes
from rhi_gateway.body import Forwarded
from rhi_gateway.errors import WireFormat
from rhi_gateway.formats.chat import ChatForwarded
from rhi_gateway.routes import Route
from rhi_gateway.usage import chat, messages, responses

# Seconds (section 6.2, step 7). Each is set: none is left to the library's default.
CONNECT_SECONDS = 10.0
WRITE_SECONDS = 60.0
READ_SECONDS = 1200.0
POOL_SECONDS = 10.0
KEEPALIVE_EXPIRY_SECONDS = 5.0

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


class _AnswerMeter:
    """An answer that is not a stream: forwarded as it comes, read once it is whole."""

    def __init__(self, route: Route) -> None:
        self._route = route
        self._parts: list[bytes] = []

    def feed(self, chunk: bytes) -> bytes:
        self._parts.append(chunk)
        return chunk

    def finish(self) -> bytes:
        return b""

    def observed(self) -> Observed:
        return read_whole_answer(self._route, b"".join(self._parts))


def _meter(route: Route, forwarded: Forwarded, content_type: str) -> _Meter:
    if content_type.split(";", 1)[0].strip().lower() != STREAM_TYPE:
        return _AnswerMeter(route)
    if route.wire_format is WireFormat.MESSAGES:
        return _MessagesMeter()
    if route.wire_format is WireFormat.RESPONSES:
        return _ResponsesMeter()
    asked = isinstance(forwarded, ChatForwarded) and forwarded.client_asked_for_usage
    return _ChatMeter(client_asked_for_usage=asked)


class Exchange:
    """One upstream answer, open: its status and type, its chunks for the client, and its usage."""

    def __init__(self, response: httpx.Response, meter: _Meter) -> None:
        self._response = response
        self._meter = meter
        self._complete = False

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def content_type(self) -> str | None:
        return self._response.headers.get("content-type")

    async def chunks(self) -> AsyncGenerator[bytes]:
        """The bytes for the client, each as soon as the upstream chunk that carries it has arrived."""
        try:
            # Decoded, should the engine compress what it was asked to send as it is: the client is
            # given no `Content-Encoding`, and the parser reads text.
            async for chunk in self._response.aiter_bytes():
                forward = self._meter.feed(chunk)
                if forward:
                    yield forward
        except (httpx.TransportError, httpx.DecodingError):
            # The upstream broke off: the answer is cut where it stands, and did not end cleanly.
            return
        self._complete = True
        rest = self._meter.finish()
        if rest:
            yield rest

    @property
    def observed(self) -> Observed:
        """The tokens observed so far; clean only for a whole, successful answer that reported its usage."""
        seen = self._meter.observed()
        clean = seen.ended_cleanly and self._complete and self._response.is_success
        return Observed(seen.input_tokens, seen.output_tokens, ended_cleanly=clean)

    async def aclose(self) -> None:
        await self._response.aclose()


class Proxy:
    """Sends filtered requests to the pools behind one address, each with its pool's secret.

    `base_url` is the internal load balancer's address and `secrets` the bearer secret of each
    pool, by the pool's name: both are configuration, never anything a client sent.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str, secrets: Mapping[str, str]) -> None:
        self._client = client
        self._base_url = base_url
        self._secrets = dict(secrets)

    async def open(self, route: Route, forwarded: Forwarded) -> Exchange:
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
        response = await self._client.send(request, stream=True)
        return Exchange(response, _meter(route, forwarded, response.headers.get("content-type", "")))


class ProxiedResponse(StreamingResponse):
    """The client's answer: the exchange's chunks, then `on_end` with what was observed, exactly once.

    `on_end` runs however the answer ends, the client gone included, and is shielded from the
    cancellation that a client's disconnect brings, so that the settlement it makes is not lost.
    """

    def __init__(self, exchange: Exchange, on_end: Callable[[Observed], Awaitable[None]]) -> None:
        self._chunks = exchange.chunks()
        super().__init__(self._chunks, status_code=exchange.status_code, media_type=exchange.content_type)
        self._exchange = exchange
        self._on_end = on_end

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            ending = asyncio.ensure_future(self._end())
            _ENDING.add(ending)
            ending.add_done_callback(_ENDING.discard)
            await asyncio.shield(ending)

    async def _end(self) -> None:
        try:
            await self._chunks.aclose()
            await self._exchange.aclose()
        finally:
            await self._on_end(self._exchange.observed)


# The ends under way, held until each is done: a task the loop alone refers to may be collected.
_ENDING: set[asyncio.Future[None]] = set()
