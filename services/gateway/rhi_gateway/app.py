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
gives: the pool, whose cookie the reservation returns. It is still made before anything is sent
to the engine. Right after it, the
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

**The session** (sections 5.6 and 6.2, step 5): a pool is served while its session is active, and at
no other time, for every key, whoever booked the session. A request names its pool in its body, so
the check is made twice. As the handler's first step, while no pool's session is active, the
request ends with the cause `no_session`, in its wire format, before its body is read. Then, as
soon as the body filter has named the pool (`rhi_gateway.body.Admit`), a request for a pool whose
session is not active ends with `no_session`: before the filter judges any other field and before
the reservation, so no slot is taken. Whether a session is active is the shared
rule (`rhi_common.sessions.is_active`) on the session the gateway last read for the pool
(`rhi_gateway.readiness.PoolView.session`), at the time of the task's own clock (`Pools.clock`)
and under the longest session of the configuration (`Pools.session_max_hours`).

**No hold** (sections 5.3 and 6.2, step 5): a request never starts a machine, so the gateway holds
none. In that same check, as soon as the body has named the pool, a request for a pool whose
session is active and that has no healthy target is answered at once, before a slot is taken:
`warming` when the scaler's note counts at least one instance starting, `capacity_unavailable`
when it counts none or the gateway keeps no note. Whether the pool can serve is read from the
values the gateway keeps of it (`rhi_gateway.readiness.PoolView.refusal`), never from AWS. Each of
the three is a 503 in the request's wire format, for a stream as for any other call: `warming` and
`capacity_unavailable` with `Retry-After`, whose seconds are the design's or those
`Forwarding.retry_after` gives, and `no_session` with none. A token count goes through the same
check, so it gets the answer a request would get. Every request the check lets through is
forwarded as it is (`rhi_gateway.proxy.Proxy.forward`): the gateway answers no request 200 before
the engine does, sends no keep-alive of its own and asks the scaler for nothing.

**In flight** (section 7.2): with the task's count (`rhi_gateway.heartbeat`), a request counts as
in flight from its reservation to its end, which the settlement marks. Token counting is never
counted, and neither is anything the local routes answer. `build_app` given the readiness reader
runs its cycle for as long as the application does.

The models, status and hello routes are answered by `rhi_gateway.local`: `build_app` fills
their slots from the `LocalRoutes` it is given.

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
them (on `Forwarding.clock`). An exception that escapes a handler is answered 500 with no body and logged with
the cause `none`.

**The metrics** (sections 6.6 and 7.2): `build_app` given the gateway's metrics
(`rhi_gateway.metrics.Metrics`) counts each of these requests once, from its finished record: per
student by alias and per pool, the requests, the tokens settled and the errors by cause.

**What students experience** (section 7.2): `build_app` given the metrics also puts on their
registry the series of `rhi_gateway.experience`, per pool and wire format and never by student,
and with the task's count the requests in flight as its rows report them. A forwarded
request tells them, once its reservation is made, which is when it is accepted, whether it is a
first turn (`first_turn`); no request is held, so each is told as a warm one. Then
the proxy tells them each time a token of its stream has been sent to the client, and whether it
is one of the answer or of the reasoning before it; and its settlement, the tokens it observed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
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
from rhi_gateway import routes, telemetry
from rhi_gateway.affinity import Affinity
from rhi_gateway.body import Admit, BodySource, Forwarded
from rhi_gateway.budget import Ledger, Reservation
from rhi_gateway.errors import GatewayError, WireFormat, render_json
from rhi_gateway.experience import Experience
from rhi_gateway.proxy import Observed, Proxy, _to_the_end
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


def _running(readiness: Readiness) -> Callable[[Starlette], Any]:
    """The application's lifespan: the pools are read while it runs."""

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        reading = asyncio.ensure_future(readiness.run())
        try:
            yield
        finally:
            reading.cancel()
            await asyncio.wait([reading])

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

    `heartbeat`, when given, is this task's count of requests in flight, which a gauge reads.
    `readiness`, when given, reads every pool on its cycle for as long (`rhi_gateway.readiness`):
    without it, whoever builds the application runs the cycle.

    `metrics`, when given, counts every request the application answers, the local routes, the
    refusals and the paths outside the table included (`rhi_gateway.metrics`). Its registry then
    also holds what students experience (`rhi_gateway.experience`), which the forwarded routes
    report, with the task's own count of the requests in flight when it is given too,
    in `wait_buckets` and `speed_buckets` (the settings' own, with the configured floors).

    `local`, when given, answers the models, status and hello routes (`rhi_gateway.local`); a
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
        lifespan=None if readiness is None else _running(readiness),
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
    """The body filter of one route: `rhi_gateway.body.BodyFilter` or a format's own.

    It calls `admit` with the pool as soon as the body has named it, before it judges any other
    field, and what `admit` raises ends the read.
    """

    async def read(
        self, source: BodySource, *, student: str, request_id: str, admit: Admit | None = None
    ) -> Forwarded: ...


@dataclass(frozen=True)
class Pools:
    """What the request path reads of the pools (section 5.3): their kept views, and the clock.

    `views` gives the kept view of every pool (`rhi_gateway.readiness.Readiness.views`); a pool it
    does not hold reads as not ready with no records. `clock` gives the seconds since the epoch,
    the clock of a session's start and end: a test gives its own. `session_max_hours` is the
    longest a session may last, the configuration's, under which each pool's session is read
    (`rhi_common.sessions`).
    """

    views: Callable[[], Mapping[str, PoolView]]
    clock: Callable[[], float] = time.time
    session_max_hours: int = NUMERIC_DEFAULTS[SESSION_MAX_HOURS]

    def view(self, pool: Pool) -> PoolView:
        """The kept view of one pool. Reads nothing."""
        return self.views().get(pool.name, PoolView())

    def refusal(self, pool: Pool) -> Cause | None:
        """Why the pool cannot serve a request now, `None` when it can (`PoolView.refusal`). Reads nothing."""
        return self.view(pool).refusal(self.clock(), session_max_hours=self.session_max_hours)

    def any_session(self) -> bool:
        """Whether any pool's session, as last read, is active now. Reads nothing."""
        now = self.clock()
        return any(view.in_session(now, session_max_hours=self.session_max_hours) for view in self.views().values())


@dataclass(frozen=True)
class Forwarding:
    """What the forwarded routes are made of: who is let in, the concurrency slots, the filters, the pools and the proxy.

    `filters` holds the body filter of each forwarded route of the table, under its entry. The
    longest a request may run is the proxy's. `heartbeat`, when given, counts the requests in flight.
    `retry_after` gives the seconds of `Retry-After` for the causes it names, in place of the
    design's (`rhi_gateway.errors.render_json`): only a cause that carries the header may be named.
    `clock` gives the seconds, on a clock that never goes back, of a request's acceptance and of
    its first token sent, for its log record (`rhi_gateway.telemetry`): a test gives its own.
    """

    authenticator: Authenticator
    ledger: Ledger
    filters: Mapping[Route, Filter]
    proxy: Proxy
    pools: Pools
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
        pools = forwarding.pools

        def admit(pool: Pool) -> None:
            # The body has named the pool: a request for one that cannot serve ends here, whatever
            # else its body holds, and nothing is reserved for it (section 6.2, step 5). The cause
            # is `no_session` for a pool whose session is not active, and otherwise, for a pool
            # with no healthy target, `warming` or `capacity_unavailable` by the scaler's note.
            telemetry.note_pool(pool.name)
            refusal = pools.refusal(pool)
            if refusal is not None:
                raise GatewayError(refusal)

        try:
            if not pools.any_session():
                # No pool is served: the request is refused before its body is read (section 6.2, step 5).
                raise GatewayError(Cause.NO_SESSION)
            forwarded = await body_filter.read(
                RequestBody(request.receive), student=str(student.github_id), request_id=request_id, admit=admit
            )
            reserve = partial(
                forwarding.ledger.reserve,
                student,
                request_id,
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
            number=pools.view(forwarded.pool).rebalance_number,
            share=forwarded.pool.rebalance_share,
        )

        demand: Demand | None = None
        if entry != routes.COUNT_TOKENS and forwarding.heartbeat is not None:
            # A token count is a cheap call: it is never counted as a request in flight (section 5.3).
            demand = forwarding.heartbeat.received(forwarded.pool.name)

        # From here the request is accepted, and settles exactly once: what students experience counts
        # its waits from this moment, and is told its tokens at its settlement.
        experience = _experience(request)
        wire_format = entry.wire_format or WireFormat.MESSAGES
        # The turn is classified before the acceptance is read: the log and the histogram read it at the same moment.
        first = experience is not None and first_turn(wire_format, forwarded)
        accepted_at = forwarding.clock()
        turn = None
        if experience is not None:
            # No request is held: the label that told a held request from a warm one reads warm on each.
            turn = experience.request(forwarded.pool.name, wire_format, held=False, first_turn=first)

        def token_sent(answer: bool) -> None:
            # The log's time to first token is read at the moments the histogram reads its own, with or without it.
            telemetry.note_first_token(forwarding.clock() - accepted_at)
            if turn is not None:
                turn.token_sent(answer)

        async def settle(observed: Observed) -> None:
            telemetry.note_tokens(observed.input_tokens, observed.output_tokens, observed.cached_input_tokens)
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

        return await forwarding.proxy.forward(
            entry, forwarded, settle, receive=request.receive, token_sent=token_sent, affinity=affinity
        )

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
