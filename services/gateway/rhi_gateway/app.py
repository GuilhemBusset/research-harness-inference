"""The gateway's Starlette application, built from the route table (system design, section 6.2, step 1).

`build_app` makes one route per entry of `rhi_gateway.routes.TABLE` and no other. Each has a
handler slot: a handler is called with the request and with the entry it serves, so a forwarder
has the entry's upstream constant at hand and no reason to read the client's path. A slot that
has no handler answers as a route outside the table does.

`forwarding_handlers` fills the four forwarded slots with the warm path of a request (section 3):
authentication from the headers, the body filter of the route's wire format, the reservation, and
the proxy (`rhi_gateway.proxy`), whose answer streams back to the client and is settled when it
ends. A clean end is charged the usage the engine reported; any other end, the tokens observed and
never less than the input estimate (`rhi_gateway.budget`). The reservation is made once the body is
read and filtered, because it needs what only the body gives: the estimate of the input, the
request's output limit, and the pool, whose hold sets how long the lease lasts. It is still made
before anything is sent to the engine.

The models, status and hello routes are answered by `rhi_gateway.local`: `build_app` fills their
slots from the `LocalRoutes` it is given.

Everything outside the table is answered 404 with the cause `not_found`, by this application and
not by Starlette: no redirect to or from a trailing slash, and no 405 for another method on a
path of the table, which would tell a client that the path exists.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
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
from rhi_gateway import routes
from rhi_gateway.body import BodySource, Forwarded
from rhi_gateway.budget import Ledger
from rhi_gateway.errors import GatewayError, WireFormat, render_json
from rhi_gateway.proxy import Observed, ProxiedResponse, Proxy
from rhi_gateway.routes import TABLE, Route, match

if TYPE_CHECKING:
    from rhi_gateway.auth import Authenticator
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


def build_app(handlers: Mapping[Route, Handler] | None = None, *, local: LocalRoutes | None = None) -> Starlette:
    """The gateway's application: the table's routes, each with the handler given for it.

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

    app = Starlette(routes=[TableRoute(entry, given.get(entry)) for entry in TABLE])
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


# The longest a request may run, in seconds (sections 6.2, step 7, and 17). A lease outlasts it.
LONGEST_REQUEST_SECONDS = 1800

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


@dataclass(frozen=True)
class Forwarding:
    """What the forwarded routes are made of: who is let in, the budget, the filters and the proxy.

    `filters` holds the body filter of each forwarded route of the table, under its entry.
    """

    authenticator: Authenticator
    ledger: Ledger
    filters: Mapping[Route, Filter]
    proxy: Proxy
    longest_request_seconds: int = LONGEST_REQUEST_SECONDS


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


def _refusal(error: GatewayError, entry: Route) -> Response:
    answer = render_json(error, entry.wire_format or WireFormat.MESSAGES)
    return Response(content=answer.body, status_code=answer.status, headers=answer.headers)


def _forward(forwarding: Forwarding, body_filter: Filter) -> Callable[[Request, Route, KeyRecord], Awaitable[Response]]:
    async def handler(request: Request, entry: Route, student: KeyRecord) -> Response:
        request_id = uuid.uuid4().hex
        try:
            forwarded = await body_filter.read(
                RequestBody(request.receive), student=str(student.github_id), request_id=request_id
            )
            reservation = await run_in_threadpool(
                forwarding.ledger.reserve,
                student,
                request_id,
                forwarded.body,
                output_limit=output_limit(entry, forwarded),
                hold_seconds=forwarded.pool.hold_seconds,
                longest_request_seconds=forwarding.longest_request_seconds,
            )
        except GatewayError as refused:
            return _refusal(refused, entry)

        async def settle(observed: Observed) -> None:
            await run_in_threadpool(
                forwarding.ledger.settle,
                reservation,
                input_tokens=observed.input_tokens,
                output_tokens=observed.output_tokens,
                ended_cleanly=observed.ended_cleanly,
            )

        try:
            exchange = await forwarding.proxy.open(entry, forwarded)
        except BaseException:
            # No answer came: the slot is freed and the input estimate charged.
            await settle(Observed(0, 0, ended_cleanly=False))
            raise
        return ProxiedResponse(exchange, settle)

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
