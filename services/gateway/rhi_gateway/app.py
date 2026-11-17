"""The gateway's Starlette application, built from the route table (system design, section 6.2, step 1).

`build_app` makes one route per entry of `rhi_gateway.routes.TABLE` and no other. Each has a
handler slot: a handler is called with the request and with the entry it serves, so a forwarder
has the entry's upstream constant at hand and no reason to read the client's path. A slot that
has no handler answers as a route outside the table does.

`forwarding_handlers` fills the four forwarded slots with the warm path of a request (section 3):
authentication from the headers, the body filter of the route's wire format, the reservation, and
the proxy (`rhi_gateway.proxy`), whose answer goes back to the client. The proxy settles every end
of the request exactly once: a clean end is charged the usage the engine reported; any other end (a
client that leaves, an engine that fails, the time limit), the tokens observed and never less than
the input estimate (`rhi_gateway.budget`). An engine that fails is told to the client with the
cause `engine_error`, and the time limit with `time_limit`, in the request's wire format. The lease
is reserved for the proxy's own time limit, so that the two cannot differ.

The reservation is made once the body is read and filtered, because it needs what only the body
gives: the estimate of the input, the request's output limit, and the pool, whose hold sets how
long the lease lasts. It is still made before anything is sent to the engine.

**The hold** (sections 5.3 and 6.2, step 5) runs between the body filter and the proxy. Whether
the pool can serve is read from the values the gateway keeps of it (`rhi_gateway.readiness`),
never from AWS, and what to do is the hold's rule (`rhi_gateway.hold`), stepped on the gateway's
clock at each moment the rule names and at least once a second. The rule's first step is taken
before the reservation: a request it ends at once (`pool_disabled`, `pool_ceiling_reached`,
`pool_scheduled_only`) is refused with nothing reserved, and a pool already ready is forwarded with
no hold. Otherwise the reservation is made, and only then is the scaler asked to wake the pool
(`rhi_gateway.waker.Waker.ask`), at once and every 15 seconds while the request is held:

- a stream is answered 200 at once with the keep-alives of its format, and the engine's stream
  follows on the same connection (`rhi_gateway.proxy.Proxy.held`);
- a call that is not a stream is held in silence, then forwarded as any other; a client that
  leaves meanwhile ends its hold, and so does the cancellation of the request, which is settled still.

A hold that ends, at its deadline (`warming`, or `capacity_unavailable`) or for any other cause,
settles the reservation with nothing observed, which the ledger charges the input estimate, and
ends the request with its cause: the error event of its format on a stream, the JSON answer (503
with `Retry-After` for `warming`) otherwise. Token counting is a cheap call (section 5.3): it is
never held and never asks for a wake, and on a pool that is not ready it is answered `warming` at
once, before any reservation.

**The demand** (section 5.1): with a heartbeat (`rhi_gateway.heartbeat`), a request counts as
held while it is held and as in flight from its forwarding to its end, which the settlement marks;
it is received once its reservation is made. Token counting is never demand, and neither is
anything the local routes answer. `build_app` given the heartbeat starts its writes with the
application and, at shutdown, stops them with a last row of zeros.

The models, status, wake and hello routes are answered by `rhi_gateway.local`: `build_app` fills
their slots from the `LocalRoutes` it is given (the wake's only when that is given the waker and
the ledger).

Everything outside the table is answered 404 with the cause `not_found`, by this application and
not by Starlette: no redirect to or from a trailing slash, and no 405 for another method on a
path of the table, which would tell a client that the path exists.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import URLPath
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Match, NoMatchFound
from starlette.types import Receive, Scope, Send

from rhi_common.causes import Cause
from rhi_common.pool_config import Pool
from rhi_common.records import Mode
from rhi_gateway import routes
from rhi_gateway.body import BodySource, Forwarded
from rhi_gateway.budget import Ledger
from rhi_gateway.errors import GatewayError, WireFormat, render_json
from rhi_gateway.hold import DEFAULT_KEEPALIVE_SECONDS, Action, End, Forward, Hold, KeepAlive, Wake
from rhi_gateway.proxy import CLIENT_LEFT_STATUS, Observed, Proxy, _to_the_end
from rhi_gateway.readiness import PoolView
from rhi_gateway.routes import TABLE, Route, match

if TYPE_CHECKING:
    from rhi_gateway.auth import Authenticator
    from rhi_gateway.heartbeat import Demand, Heartbeat
    from rhi_gateway.keystore import KeyRecord
    from rhi_gateway.local import LocalRoutes

# What fills a slot: it gets the request and the table entry it was registered for.
Handler = Callable[[Request, Route], Awaitable[Response]]

# A request outside the table has no wire format of its own. Both error shapes carry the cause at
# `error.type` and the sentence at `error.message`, so one shape is read by every client.
_NOT_FOUND = render_json(Cause.NOT_FOUND, WireFormat.MESSAGES)


async def not_found(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer 404 with the cause `not_found`."""
    response = Response(content=_NOT_FOUND.body, status_code=_NOT_FOUND.status, headers=_NOT_FOUND.headers)
    await response(scope, receive, send)


class TableRoute(BaseRoute):
    """The Starlette route of one table entry: it matches that entry's requests and nothing else."""

    def __init__(self, entry: Route, handler: Handler | None = None) -> None:
        self.entry = entry
        self.handler = handler

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        # Never a partial match: Starlette answers one with 405.
        if scope["type"] == "http" and match(scope["method"], scope.get("raw_path")) == self.entry:
            return Match.FULL, {}
        return Match.NONE, {}

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.handler is None:
            await not_found(scope, receive, send)
            return
        response = await self.handler(Request(scope, receive, send), self.entry)
        await response(scope, receive, send)

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        # The table's routes have no names: the gateway builds no URL of its own from them.
        raise NoMatchFound(name, path_params)


def _beating(heartbeat: Heartbeat) -> Callable[[Starlette], Any]:
    """The application's lifespan: the heartbeat writes while it runs, and its last row is one of zeros."""

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        writing = asyncio.ensure_future(heartbeat.run())
        try:
            yield
        finally:
            writing.cancel()
            await asyncio.wait((writing,))
            await heartbeat.stop()

    return lifespan


def build_app(
    handlers: Mapping[Route, Handler] | None = None,
    *,
    local: LocalRoutes | None = None,
    heartbeat: Heartbeat | None = None,
) -> Starlette:
    """The gateway's application: the table's routes, each with the handler given for it.

    `heartbeat`, when given, writes this task's demand for as long as the application runs.

    `local`, when given, answers the models, status, wake and hello routes (`rhi_gateway.local`); a
    handler given for one of them as well is refused. A handler given for anything that is not an
    entry of the table is refused: the table is the only way a route comes to exist.
    """
    given = dict(handlers or {})
    if local is not None:
        served = local.handlers()
        if given.keys() & served.keys():
            raise ValueError("a handler was given for a route that the local routes answer")
        given.update(served)
    if any(entry not in TABLE for entry in given):
        raise ValueError("a handler was given for a route that is not in the table")

    app = Starlette(
        routes=[TableRoute(entry, given.get(entry)) for entry in TABLE],
        lifespan=None if heartbeat is None else _beating(heartbeat),
    )
    router = app.router
    router.redirect_slashes = False
    close_websocket = router.default

    async def outside_the_table(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await not_found(scope, receive, send)
        else:
            # A WebSocket: Starlette's own fallback closes it.
            await close_websocket(scope, receive, send)

    router.default = outside_the_table
    return app


# Where each format's body carries its output limit, the first of them that holds a number counting.
_OUTPUT_LIMIT_FIELDS = {
    WireFormat.MESSAGES: ("max_tokens",),
    WireFormat.CHAT_COMPLETIONS: ("max_completion_tokens", "max_tokens"),
    WireFormat.RESPONSES: ("max_output_tokens",),
}


class RequestBody:
    """A request's body as the filter reads it (`rhi_gateway.body.BodySource`), from ASGI messages."""

    def __init__(self, receive: Receive) -> None:
        self._receive = receive
        self._held = b""
        self._more = True

    async def _pull(self) -> None:
        while not self._held and self._more:
            message = await self._receive()
            if message["type"] != "http.request":
                # The client left: what it sent is all there is.
                self._more = False
                return
            self._held = message.get("body", b"")
            self._more = message.get("more_body", False)

    async def read(self, size: int) -> bytes:
        await self._pull()
        given, self._held = self._held[:size], self._held[size:]
        return given

    async def at_end(self) -> bool:
        await self._pull()
        return not self._held and not self._more


class Filter(Protocol):
    """The body filter of one route: `rhi_gateway.body.BodyFilter` or a format's own."""

    async def read(self, source: BodySource, *, student: str, request_id: str) -> Forwarded: ...


# How often a held request reads the kept view of its pool, in seconds, besides the moments the
# hold's rule names. The views change once a readiness cycle (5 seconds), so a pool found ready is
# forwarded to within a second of the gateway learning it.
HOLD_READ_SECONDS = 1.0

# The mode of a pool for which the admin wrote none, by its `wake` (section 11.3), as the status reads it.
_CONFIGURED_MODE = {"on-demand": Mode.ENABLED, "scheduled-only": Mode.SCHEDULED_ONLY}


@dataclass(frozen=True)
class Holding:
    """What the hold reads and asks (section 5.3): the kept views of the pools, and the wake.

    `views` gives the kept view of every pool (`rhi_gateway.readiness.Readiness.views`); a pool it
    does not hold reads as not ready with no records. `wake` asks the scaler to run for a pool and
    returns at once (`rhi_gateway.waker.Waker.ask`). `clock` gives the seconds since the epoch, the
    clock of the scaler's note, and `sleep` waits on it: a test gives its own.
    """

    views: Callable[[], Mapping[str, PoolView]]
    wake: Callable[[str], object]
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS

    def view(self, pool: Pool) -> PoolView:
        """The kept view of one pool. Reads nothing."""
        return self.views().get(pool.name, PoolView())


@dataclass(frozen=True)
class Forwarding:
    """What the forwarded routes are made of: who is let in, the budget, the filters, the hold and the proxy.

    `filters` holds the body filter of each forwarded route of the table, under its entry. The
    longest a request may run is the proxy's. `heartbeat`, when given, counts the demand.
    """

    authenticator: Authenticator
    ledger: Ledger
    filters: Mapping[Route, Filter]
    proxy: Proxy
    holding: Holding
    heartbeat: Heartbeat | None = None


def output_limit(entry: Route, forwarded: Forwarded) -> int:
    """The output a request may produce, which its reservation counts: none for a token count.

    The body is the filter's, so the number in it is already clamped to the pool's largest output;
    a body that names none may produce that much.
    """
    if entry == routes.COUNT_TOKENS or entry.wire_format is None:
        return 0
    body = json.loads(forwarded.body)
    for name in _OUTPUT_LIMIT_FIELDS[entry.wire_format]:
        value = body.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, math.ceil(value))
    return forwarded.pool.largest_output


def _streams(forwarded: Forwarded) -> bool:
    """Whether the client asked for a stream: the three formats name it `stream`, and the filter kept it."""
    return json.loads(forwarded.body).get("stream") is True


class _Held:
    """One request's hold (`rhi_gateway.hold`), on the gateway's clock and its kept view of the pool.

    Made before the reservation, with the hold's first step: a request the rule ends at once is
    refused then and reserves nothing, and a pool already ready is forwarded with no hold. What
    else the first step asks (the wake) is done only once the reservation is made, by `waiting`:
    a request the budget refuses never reaches the scaler (section 5.1).
    """

    def __init__(self, holding: Holding, pool: Pool, wire_format: WireFormat, *, streams: bool) -> None:
        self._holding = holding
        self._pool = pool
        self.streams = streams
        self._hold = Hold(
            pool,
            wire_format,
            streams=streams,
            started_at=holding.clock(),
            keepalive_seconds=holding.keepalive_seconds,
        )
        self._first = self._step()
        for action in self._first:
            if isinstance(action, End):
                raise GatewayError(action.cause)
        self.ready = any(isinstance(action, Forward) for action in self._first)
        self.demand: Demand | None = None  # Set once the request is received: it is forwarded when the hold ends.

    def _step(self) -> tuple[Action, ...]:
        view = self._holding.view(self._pool)
        mode = _CONFIGURED_MODE[self._pool.wake] if view.mode is None else view.mode.mode
        return self._hold.step(self._holding.clock(), mode=mode, note=view.note, ready=view.ready)

    async def _until_next(self) -> None:
        """Sleep until the rule has something to do, or the next read of the view, whichever comes first."""
        due = self._hold.next_at
        wait = HOLD_READ_SECONDS if due is None else min(due - self._holding.clock(), HOLD_READ_SECONDS)
        await self._holding.sleep(max(0.0, wait))

    async def waiting(self) -> AsyncGenerator[bytes]:
        """The hold: its keep-alives, each as it is due, and its wakes, until the pool is ready.

        Ends when the request may be forwarded; raises `GatewayError` with the cause that ends it.
        """
        actions, self._first = self._first, ()
        while True:
            for action in actions:
                if isinstance(action, KeepAlive):
                    yield action.data
                elif isinstance(action, Wake):
                    self._holding.wake(self._pool.name)
                elif isinstance(action, Forward):
                    if self.demand is not None:
                        self.demand.forwarded()
                    return
                else:
                    raise GatewayError(action.cause)
            # A pool found ready since the last step is acted on at once, never after one more wait:
            # the hold may first run, or resume from a keep-alive, well after its last step.
            if not self._holding.view(self._pool).ready:
                await self._until_next()
            actions = self._step()

    async def next_keep_alive(self) -> bytes:
        """Once forwarded: the next keep-alive, when it is due. None follows the engine's first event."""
        while True:
            due = self._hold.next_at
            if due is None:
                await asyncio.get_running_loop().create_future()  # Never set: the keep-alives are over.
                continue
            await self._holding.sleep(max(0.0, due - self._holding.clock()))
            for action in self._step():
                if isinstance(action, KeepAlive):
                    return action.data

    def first_event(self) -> None:
        self._hold.first_event()


async def _until_disconnect(receive: Receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


async def _hold_silently(held: _Held, receive: Receive) -> bool:
    """Hold a call that is not a stream, with nothing sent; whether the client left meanwhile.

    Raises `GatewayError` with the cause that ends the hold.
    """

    async def hold() -> None:
        async with aclosing(held.waiting()) as waiting:
            async for _ in waiting:
                pass  # A call that is not a stream is given no keep-alive.

    holding = asyncio.ensure_future(hold())
    left = asyncio.ensure_future(_until_disconnect(receive))
    try:
        await asyncio.wait((holding, left), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (holding, left):
            task.cancel()
        await asyncio.wait((holding, left))
    if holding.done() and not holding.cancelled():
        holding.result()
        return False
    return True


def _refusal(error: GatewayError, entry: Route) -> Response:
    answer = render_json(error, entry.wire_format or WireFormat.MESSAGES)
    return Response(content=answer.body, status_code=answer.status, headers=answer.headers)


def _forward(forwarding: Forwarding, body_filter: Filter) -> Callable[[Request, Route, KeyRecord], Awaitable[Response]]:
    async def handler(request: Request, entry: Route, student: KeyRecord) -> Response:
        request_id = uuid.uuid4().hex
        holding = forwarding.holding
        held: _Held | None = None
        try:
            forwarded = await body_filter.read(
                RequestBody(request.receive), student=str(student.github_id), request_id=request_id
            )
            if entry == routes.COUNT_TOKENS:
                # A cheap call: never held, never a wake, never demand (section 5.3).
                if not holding.view(forwarded.pool).ready:
                    raise GatewayError(Cause.WARMING)
            else:
                held = _Held(
                    holding, forwarded.pool, entry.wire_format or WireFormat.MESSAGES, streams=_streams(forwarded)
                )
            reservation = await run_in_threadpool(
                forwarding.ledger.reserve,
                student,
                request_id,
                forwarded.body,
                output_limit=output_limit(entry, forwarded),
                hold_seconds=forwarded.pool.hold_seconds,
                longest_request_seconds=forwarding.proxy.longest_request_seconds,
            )
        except GatewayError as refused:
            return _refusal(refused, entry)

        demand: Demand | None = None
        if held is not None and forwarding.heartbeat is not None:
            # Token counting has no hold: it is never demand.
            demand = held.demand = forwarding.heartbeat.received(forwarded.pool.name, held=not held.ready)

        async def settle(observed: Observed) -> None:
            if demand is not None:
                demand.end()
            await run_in_threadpool(
                forwarding.ledger.settle,
                reservation,
                input_tokens=observed.input_tokens,
                output_tokens=observed.output_tokens,
                ended_cleanly=observed.ended_cleanly,
            )

        if held is not None and not held.ready:
            if held.streams:
                # Answered 200 at once: the keep-alives, then the engine's stream on the same connection.
                return forwarding.proxy.held(entry, forwarded, settle, held)
            try:
                left = await _hold_silently(held, request.receive)
            except GatewayError as ended:
                await settle(Observed(0, 0, ended_cleanly=False))
                return _refusal(ended, entry)
            except asyncio.CancelledError:
                # The request itself is cancelled while held: it has ended, and its settlement is run to its end still.
                await _to_the_end(settle(Observed(0, 0, ended_cleanly=False)))
                raise
            if left:
                await settle(Observed(0, 0, ended_cleanly=False))
                return Response(status_code=CLIENT_LEFT_STATUS)
        return await forwarding.proxy.forward(entry, forwarded, settle, receive=request.receive)

    return handler


def forwarding_handlers(forwarding: Forwarding) -> dict[Route, Handler]:
    """The handlers of the four forwarded routes, each behind authentication."""
    # Imported here: `rhi_gateway.auth` imports this module for `Handler`.
    from rhi_gateway.auth import authenticated

    forwarded = [entry for entry in TABLE if entry.upstream_path is not None]
    if set(forwarding.filters) != set(forwarded):
        raise ValueError("a body filter is needed for each forwarded route, and for nothing else")
    return {
        entry: authenticated(forwarding.authenticator, _forward(forwarding, forwarding.filters[entry]))
        for entry in forwarded
    }
