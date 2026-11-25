"""The gateway's Starlette application, built from the route table (system design, section 6.2, step 1).

`build_app` makes one route per entry of `rhi_gateway.routes.TABLE` and no other. Each has a
handler slot: a handler is called with the request and with the entry it serves, so a forwarder
has the entry's upstream constant at hand and no reason to read the client's path. A slot that
has no handler answers as a route outside the table does.

`forwarding_handlers` fills the four forwarded slots with the warm path of a request (section 3):
authentication from the headers, the body filter of the route's wire format, the reservation, and
the proxy (`rhi_gateway.proxy`), whose answer goes back to the client. The proxy settles every end
of the request exactly once, a clean end or any other (a client that leaves, an engine that fails,
the time limit): the settlement frees the request's concurrency slot and charges nothing
(`rhi_gateway.budget`). An engine that fails is told to the client with the cause `engine_error`,
and the time limit with `time_limit`, in the request's wire format. The lease is taken for the
proxy's own time limit, so that the two cannot differ.

The reservation is made once the body is read and filtered, because it needs what only the body
gives: the pool, whose hold sets how long the lease lasts, and whose cookie the reservation
returns. It is still made before anything is sent to the engine. Right after it, the
key is read once more past the cache (`rhi_gateway.auth.Authenticator.confirm`): a key revoked or
erased while the body arrived, a read that fails, or a request cancelled before the read ends, has
the reservation taken back, and the day's row deleted when it holds only what its opening carried
from the day before (`Ledger.withdraw`), so that no request undoes an erase and none takes what
another request holds or stored. The reservation names the pool, and returns the load
balancer's cookie the student's record holds for it: the proxy sends that cookie, and the
settlement stores the one the request ended with (`rhi_gateway.affinity`). It returns the last
rebalance number the record has applied for the pool as well: the request is
judged on the pool's number as the gateway holds it then, which may drop the cookie for this
request, and the settlement stores that number (section 5.5).

**The hold** (sections 5.3 and 6.2, step 5) runs between the body filter and the proxy. Whether
the pool can serve is read from the values the gateway keeps of it (`rhi_gateway.readiness`),
never from AWS, and what to do is the hold's rule (`rhi_gateway.hold`), stepped on the gateway's
clock at each moment the rule names and at least once a second. The rule's first step is taken
before the reservation: a request it ends at once (`pool_disabled`, `pool_scheduled_only`) is
refused with nothing reserved, and a pool already ready is forwarded with no hold. Otherwise the
reservation is made, and only then is the scaler asked to wake the pool
(`rhi_gateway.waker.Waker.ask`), at once and every 15 seconds while the request is held:

- a stream is answered 200 at once with the keep-alives of its format, and the engine's stream
  follows on the same connection (`rhi_gateway.proxy.Proxy.held`);
- a call that is not a stream is held in silence, then forwarded as any other; a client that
  leaves meanwhile ends its hold, and so does the cancellation of the request, which is settled still.

Either answer carries the header `x-rhi-held: true` (`rhi_gateway.hold.HELD_HEADER`), which a
request forwarded with no hold never does: a hold that ends before its first keep-alive leaves no
other sign a client can see.

A hold that ends, at its deadline (`warming`, or `capacity_unavailable`) or for any other cause,
settles the reservation with nothing observed, which frees its slot, and
ends the request with its cause: the error event of its format on a stream, the JSON answer (503
with `Retry-After` for `warming`) otherwise. The seconds of `Retry-After` are the design's for each
cause that carries the header, or those `Forwarding.retry_after` gives. Token counting is a cheap
call (section 5.3): it is never held and never asks for a wake, and on a pool that is not ready it
is answered `warming` at once, before any reservation.

**The session** (sections 5.6 and 6.2, step 5): a pool is served while its session is active, and at
no other time, for every key, whoever booked the session. A request names its pool in its body, so
the check is made twice. As the handler's first step, while no pool's session is active, the
request ends with the cause `no_session`, in its wire format, before its body is read. Then, once
the body filter has named the pool, a request for a pool whose session is not active ends with
`no_session`: before the hold's first step and before the reservation, so no slot is taken, nothing
is held or woken and the scaler sees no demand from it. Whether a session is active is the shared
rule (`rhi_common.sessions.is_active`) on the session the gateway last read for the pool
(`rhi_gateway.readiness.PoolView.session`), at the time of the task's own clock (`Holding.clock`)
and under the longest session of the configuration (`Holding.session_max_hours`).

**The demand** (section 5.1): with a heartbeat (`rhi_gateway.heartbeat`), a request counts as
held while it is held and as in flight from its forwarding to its end, which the settlement marks;
it is received once its reservation is made. Token counting is never demand, and neither is
anything the local routes answer. `build_app` given the heartbeat starts its writes with the
application and, at shutdown, stops them with a last row of zeros. Given the readiness reader, it
runs its cycle for as long as the application does.

The models, status, wake and hello routes are answered by `rhi_gateway.local`: `build_app` fills
their slots from the `LocalRoutes` it is given (the wake's only when that is given the waker).

Everything outside the table is answered 404 with the cause `not_found`, by this application and
not by Starlette: no redirect to or from a trailing slash, and no 405 for another method on a
path of the table, which would tell a client that the path exists.

**The log** (sections 6.2, 6.6 and 7.3): every request the application answers, a local route, a
refusal and a path outside the table included, gives one log record of metadata
(`rhi_gateway.telemetry`), written by the middleware `build_app` installs. Each part that decides
something of a request notes it there: the alias once the key is accepted, the pool once the body
names it, the tokens at the settlement, and the cause wherever an error is rendered. A forwarded
request also notes how long it waited, whether or not the gateway has metrics: the time from its
acceptance to its first token sent, read at the two moments the time-to-first-token histogram reads
them (on `Forwarding.clock`), and the time it was held, from the hold's start to its end, on the
hold's own clock. An exception that escapes a handler is answered 500 with no body and logged with
the cause `none`.

**The metrics** (sections 6.6 and 7.2): `build_app` given the gateway's metrics
(`rhi_gateway.metrics.Metrics`) counts each of these requests once, from its finished record: per
student by alias and per pool, the requests, the tokens settled and the errors by cause.

**What students experience** (section 7.2): `build_app` given the metrics also puts on their
registry the series of `rhi_gateway.experience`, per pool and wire format and never by student,
and with the heartbeat the requests in flight and held as its rows report them. A forwarded
request tells them, once its reservation is made, which is when it is accepted, whether it is held
(its pool was not ready, so it waits for it) and whether it is a first turn (`first_turn`). Then
the proxy tells them each time a token of its stream has been sent to the client, and whether it
is one of the answer or of the reasoning before it; and its settlement, the tokens it observed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import URLPath
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Match, NoMatchFound
from starlette.types import Receive, Scope, Send

from rhi_common.boundaries import SPEED_BUCKETS, WAIT_BUCKETS
from rhi_common.causes import Cause
from rhi_common.pool_config import Pool
from rhi_common.records import Mode
from rhi_gateway import routes, telemetry
from rhi_gateway.affinity import Affinity
from rhi_gateway.body import BodySource, Forwarded
from rhi_gateway.budget import Ledger, Reservation
from rhi_gateway.errors import GatewayError, WireFormat, render_json
from rhi_gateway.experience import Experience
from rhi_gateway.hold import DEFAULT_KEEPALIVE_SECONDS, HELD_HEADER, Action, End, Forward, Hold, KeepAlive, Wake
from rhi_gateway.proxy import CLIENT_LEFT_STATUS, Observed, Proxy, _to_the_end
from rhi_gateway.readiness import PoolView, Readiness
from rhi_gateway.routes import TABLE, Route, match
from rhi_gateway.settings import NUMERIC_DEFAULTS, SESSION_MAX_HOURS

if TYPE_CHECKING:
    from rhi_gateway.auth import Authenticator
    from rhi_gateway.heartbeat import Demand, Heartbeat
    from rhi_gateway.keystore import KeyRecord
    from rhi_gateway.local import LocalRoutes
    from rhi_gateway.metrics import Metrics

# What fills a slot: it gets the request and the table entry it was registered for.
Handler = Callable[[Request, Route], Awaitable[Response]]

# A request outside the table has no wire format of its own. Both error shapes carry the cause at
# `error.type` and the sentence at `error.message`, so one shape is read by every client.
_NOT_FOUND = render_json(Cause.NOT_FOUND, WireFormat.MESSAGES)


async def not_found(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer 404 with the cause `not_found`."""
    telemetry.note_cause(Cause.NOT_FOUND)
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


def _running(heartbeat: Heartbeat | None, readiness: Readiness | None) -> Callable[[Starlette], Any]:
    """The application's lifespan: the pools are read and the heartbeat writes while it runs.

    The heartbeat's last row is one of zeros, written once both have stopped.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        loops = [part.run() for part in (readiness, heartbeat) if part is not None]
        running = [asyncio.ensure_future(loop) for loop in loops]
        try:
            yield
        finally:
            for task in running:
                task.cancel()
            await asyncio.wait(running)
            if heartbeat is not None:
                await heartbeat.stop()

    return lifespan


def build_app(
    handlers: Mapping[Route, Handler] | None = None,
    *,
    local: LocalRoutes | None = None,
    heartbeat: Heartbeat | None = None,
    metrics: Metrics | None = None,
    readiness: Readiness | None = None,
    wait_buckets: tuple[float, ...] = WAIT_BUCKETS,
    speed_buckets: tuple[float, ...] = SPEED_BUCKETS,
) -> Starlette:
    """The gateway's application: the table's routes, each with the handler given for it.

    `heartbeat`, when given, writes this task's demand for as long as the application runs.
    `readiness`, when given, reads every pool on its cycle for as long (`rhi_gateway.readiness`):
    without it, whoever builds the application runs the cycle.

    `metrics`, when given, counts every request the application answers, the local routes, the
    refusals and the paths outside the table included (`rhi_gateway.metrics`). Its registry then
    also holds what students experience (`rhi_gateway.experience`), which the forwarded routes
    report, with the heartbeat's own counts of the requests in flight and held when it is given too,
    in `wait_buckets` and `speed_buckets` (the settings' own, with the configured floors).

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
        lifespan=None if heartbeat is None and readiness is None else _running(heartbeat, readiness),
        # One log record per request, counted as it is logged, and the gateway's own faults answered
        # 500 with no text.
        middleware=[Middleware(telemetry.Telemetry, ended=None if metrics is None else metrics.count)],
    )
    # Where the forwarded routes find it (`_experience`): the handlers are made before the application.
    app.state.experience = (
        None
        if metrics is None
        else Experience(
            metrics.registry,
            None if heartbeat is None else heartbeat.rows,
            wait_buckets=wait_buckets,
            speed_buckets=speed_buckets,
        )
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
    clock of the scaler's note and of a session's start and end, and `sleep` waits on it: a test
    gives its own. `session_max_hours` is the longest a session may last, the configuration's,
    under which each pool's session is read (`rhi_common.sessions`).
    """

    views: Callable[[], Mapping[str, PoolView]]
    wake: Callable[[str], object]
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS
    session_max_hours: int = NUMERIC_DEFAULTS[SESSION_MAX_HOURS]

    def view(self, pool: Pool) -> PoolView:
        """The kept view of one pool. Reads nothing."""
        return self.views().get(pool.name, PoolView())

    def in_session(self, pool: Pool) -> bool:
        """Whether the pool's session, as last read, is active now (section 5.6). Reads nothing."""
        return self.view(pool).in_session(self.clock(), session_max_hours=self.session_max_hours)

    def any_session(self) -> bool:
        """Whether any pool's session, as last read, is active now. Reads nothing."""
        now = self.clock()
        return any(view.in_session(now, session_max_hours=self.session_max_hours) for view in self.views().values())


@dataclass(frozen=True)
class Forwarding:
    """What the forwarded routes are made of: who is let in, the concurrency slots, the filters, the hold and the proxy.

    `filters` holds the body filter of each forwarded route of the table, under its entry. The
    longest a request may run is the proxy's. `heartbeat`, when given, counts the demand.
    `retry_after` gives the seconds of `Retry-After` for the causes it names, in place of the
    design's (`rhi_gateway.errors.render_json`): only a cause that carries the header may be named.
    `clock` gives the seconds, on a clock that never goes back, of a request's acceptance and of
    its first token sent, for its log record (`rhi_gateway.telemetry`): a test gives its own.
    """

    authenticator: Authenticator
    ledger: Ledger
    filters: Mapping[Route, Filter]
    proxy: Proxy
    holding: Holding
    heartbeat: Heartbeat | None = None
    retry_after: Mapping[Cause, int] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        # Refused when the gateway is built, not at the first refusal it would render.
        render_json(Cause.WARMING, WireFormat.MESSAGES, self.retry_after)


def _streams(forwarded: Forwarded) -> bool:
    """Whether the client asked for a stream: the three formats name it `stream`, and the filter kept it."""
    return json.loads(forwarded.body).get("stream") is True


def first_turn(wire_format: WireFormat, forwarded: Forwarded) -> bool:
    """Whether the request's conversation carries no earlier output of the model (section 7.2).

    Messages and Chat Completions give the model's earlier turns the role `assistant`. Responses
    does too, for a message; its other input items that are the model's own are its reasoning and
    its calls of tools, whose types the reference all ends in `_call`. What answers a call
    (`function_call_output` and the like) is the client's, and never comes without its call.
    """
    body = json.loads(forwarded.body)
    conversation = body.get("input" if wire_format is WireFormat.RESPONSES else "messages")
    if not isinstance(conversation, list):
        return True  # The one text of a Responses request, or nothing at all.
    for item in conversation:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "assistant":
            return False
        kind = item.get("type") if wire_format is WireFormat.RESPONSES else None
        if isinstance(kind, str) and (kind == "reasoning" or kind.endswith("_call")):
            return False
    return True


class _Held:
    """One request's hold (`rhi_gateway.hold`), on the gateway's clock and its kept view of the pool.

    Made before the reservation, with the hold's first step: a request the rule ends at once is
    refused then and reserves nothing, and a pool already ready is forwarded with no hold. What
    else the first step asks (the wake) is done only once the reservation is made, by `waiting`:
    a request the concurrency limit refuses never reaches the scaler (section 5.1).
    """

    def __init__(self, holding: Holding, pool: Pool, wire_format: WireFormat, *, streams: bool) -> None:
        self._holding = holding
        self._pool = pool
        self.streams = streams
        self._started_at = holding.clock()
        self._hold = Hold(
            pool,
            wire_format,
            streams=streams,
            started_at=self._started_at,
            keepalive_seconds=holding.keepalive_seconds,
        )
        self._first = self._step()
        for action in self._first:
            if isinstance(action, End):
                raise GatewayError(action.cause)
        self.ready = any(isinstance(action, Forward) for action in self._first)
        self.demand: Demand | None = None  # Set once the request is received: it is forwarded when the hold ends.
        self._ended = False

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
        However it ends, the time held, from the hold's start, is noted on the request's record.
        """
        actions, self._first = self._first, ()
        try:
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
        finally:
            self.ended()

    def ended(self) -> None:
        """The hold is over: the time held, from its start, is noted on the request's record, once.

        `waiting` notes it when it ends; the settlement, for a request that ended before its hold ran.
        """
        if not self._ended:
            self._ended = True
            telemetry.note_held(self._holding.clock() - self._started_at)

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


def _experience(request: Request) -> Experience | None:
    """The experience series of the application that serves the request; none when it was built without metrics."""
    app = request.scope.get("app")
    return None if app is None else getattr(app.state, "experience", None)


def _refusal(error: GatewayError, entry: Route, retry_after: Mapping[Cause, int]) -> Response:
    telemetry.note_cause(error.cause)
    answer = render_json(error, entry.wire_format or WireFormat.MESSAGES, retry_after)
    return Response(content=answer.body, status_code=answer.status, headers=answer.headers)


# The reservations being made or taken back for requests cancelled meanwhile: held here so that
# none is collected before it ends.
_RESERVING: set[asyncio.Future[Any]] = set()


def _kept(work: Awaitable[Any]) -> asyncio.Future[Any]:
    running = asyncio.ensure_future(work)
    _RESERVING.add(running)
    running.add_done_callback(_RESERVING.discard)
    return running


async def _reserved(
    reserve: Callable[[], Reservation], confirm: Callable[[], Awaitable[KeyRecord]], ledger: Ledger
) -> tuple[Reservation, KeyRecord]:
    """Reserve, then read the key again past the cache (`Authenticator.confirm`); the reservation and the record.

    A key revoked or erased since it was accepted, a read that fails, or a request cancelled during
    the read, has the reservation taken back (`Ledger.withdraw`), the withdrawal run to its end
    even if the request is cancelled meanwhile. The reservation's write cannot be stopped once
    started: a request cancelled during it has its reservation taken back once the write ends. So
    no cancellation leaves a reservation that no settlement or withdrawal ends.
    """
    reserving = _kept(run_in_threadpool(reserve))
    try:
        reservation = await asyncio.shield(reserving)
    except asyncio.CancelledError:

        def take_back(done: asyncio.Future[Reservation]) -> None:
            if not done.cancelled() and done.exception() is None:
                _kept(run_in_threadpool(ledger.withdraw, done.result()))

        reserving.add_done_callback(take_back)
        raise
    try:
        return reservation, await confirm()
    except BaseException:
        await _to_the_end(run_in_threadpool(ledger.withdraw, reservation))
        raise


def _forward(forwarding: Forwarding, body_filter: Filter) -> Callable[[Request, Route, KeyRecord], Awaitable[Response]]:
    async def handler(request: Request, entry: Route, student: KeyRecord) -> Response:
        request_id = uuid.uuid4().hex
        holding = forwarding.holding
        held: _Held | None = None
        try:
            if not holding.any_session():
                # No pool is served: the request is refused before its body is read (section 6.2, step 5).
                raise GatewayError(Cause.NO_SESSION)
            forwarded = await body_filter.read(
                RequestBody(request.receive), student=str(student.github_id), request_id=request_id
            )
            telemetry.note_pool(forwarded.pool.name)
            if not holding.in_session(forwarded.pool):
                # The body has named the pool: nothing is reserved, held or woken for one with no active session.
                raise GatewayError(Cause.NO_SESSION)
            if entry == routes.COUNT_TOKENS:
                # A cheap call: never held, never a wake, never demand (section 5.3).
                if not holding.view(forwarded.pool).ready:
                    raise GatewayError(Cause.WARMING)
            else:
                held = _Held(
                    holding, forwarded.pool, entry.wire_format or WireFormat.MESSAGES, streams=_streams(forwarded)
                )
            reserve = partial(
                forwarding.ledger.reserve,
                student,
                request_id,
                hold_seconds=forwarded.pool.hold_seconds,
                longest_request_seconds=forwarding.proxy.longest_request_seconds,
                pool=forwarded.pool.name,
            )
            # The body may have taken long: the key is read again once the reservation is written.
            reservation, student = await _reserved(
                reserve, partial(forwarding.authenticator.confirm, student), forwarding.ledger
            )
        except GatewayError as refused:
            return _refusal(refused, entry, forwarding.retry_after)
        # The load balancer's cookie the student's record holds for the pool: the proxy sends it,
        # and the settlement stores the one the request ends with (section 5.5). The request is
        # judged here on the pool's rebalance number, as the gateway holds it now: a number new to
        # the student that selects them drops the cookie, and the settlement stores the number.
        affinity = Affinity.judged(
            student.github_id,
            reservation.cookie,
            applied=reservation.rebalance_applied,
            number=holding.view(forwarded.pool).rebalance_number,
            share=forwarded.pool.rebalance_share,
        )

        demand: Demand | None = None
        if held is not None and forwarding.heartbeat is not None:
            # Token counting has no hold: it is never demand.
            demand = held.demand = forwarding.heartbeat.received(forwarded.pool.name, held=not held.ready)

        # From here the request is accepted, and settles exactly once: what students experience counts
        # its waits from this moment, warm or held, and is told its tokens at its settlement.
        experience = _experience(request)
        wire_format = entry.wire_format or WireFormat.MESSAGES
        # The turn is classified before the acceptance is read: the log and the histogram read it at the same moment.
        first = experience is not None and first_turn(wire_format, forwarded)
        accepted_at = forwarding.clock()
        turn = None
        if experience is not None:
            turn = experience.request(
                forwarded.pool.name,
                wire_format,
                held=held is not None and not held.ready,
                first_turn=first,
            )

        def token_sent(answer: bool) -> None:
            # The log's time to first token is read at the moments the histogram reads its own, with or without it.
            telemetry.note_first_token(forwarding.clock() - accepted_at)
            if turn is not None:
                turn.token_sent(answer)

        async def settle(observed: Observed) -> None:
            telemetry.note_tokens(observed.input_tokens, observed.output_tokens, observed.cached_input_tokens)
            if held is not None and not held.ready:
                # A request that ended before its hold ran, or while it ran, was held until now.
                held.ended()
            if demand is not None:
                demand.end()
            if turn is not None:
                turn.settled(observed.input_tokens, observed.output_tokens)
            await run_in_threadpool(
                forwarding.ledger.settle,
                reservation,
                cookie=affinity.ended,
                rebalance_number=affinity.number,
                cookie_dropped=affinity.dropped,
            )

        if held is not None and not held.ready:
            if held.streams:
                # Answered 200 at once: the keep-alives, then the engine's stream on the same connection.
                answer = forwarding.proxy.held(entry, forwarded, settle, held, token_sent=token_sent, affinity=affinity)
                return _marked_held(answer)
            try:
                left = await _hold_silently(held, request.receive)
            except GatewayError as ended:
                await settle(Observed(0, 0, ended_cleanly=False))
                return _marked_held(_refusal(ended, entry, forwarding.retry_after))
            except asyncio.CancelledError:
                # The request itself is cancelled while held: it has ended, and its settlement is run to its end still.
                await _to_the_end(settle(Observed(0, 0, ended_cleanly=False)))
                raise
            if left:
                await settle(Observed(0, 0, ended_cleanly=False))
                return Response(status_code=CLIENT_LEFT_STATUS)
            answer = await forwarding.proxy.forward(
                entry, forwarded, settle, receive=request.receive, token_sent=token_sent, affinity=affinity
            )
            return _marked_held(answer)
        return await forwarding.proxy.forward(
            entry, forwarded, settle, receive=request.receive, token_sent=token_sent, affinity=affinity
        )

    return handler


def _marked_held(answer: Response) -> Response:
    """The answer to a request the gateway held, with the header that says so (`rhi_gateway.hold.HELD_HEADER`)."""
    answer.headers[HELD_HEADER] = "true"
    return answer


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
