"""The gateway's Starlette application, built from the route table (system design, section 6.2, step 1).

`build_app` makes one route per entry of `rhi_gateway.routes.TABLE` and no other. Each has a
handler slot: a handler is called with the request and with the entry it serves, so a forwarder
has the entry's upstream constant at hand and no reason to read the client's path. The handlers
themselves (authentication, the local answers, forwarding) are not written here; a slot that has
none answers as a route outside the table does.

Everything outside the table is answered 404 with the cause `not_found`, by this application and
not by Starlette: no redirect to or from a trailing slash, and no 405 for another method on a
path of the table, which would tell a client that the path exists.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import URLPath
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Match, NoMatchFound
from starlette.types import Receive, Scope, Send

from rhi_common.causes import Cause
from rhi_gateway.errors import WireFormat, render_json
from rhi_gateway.routes import TABLE, Route, match

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


def build_app(handlers: Mapping[Route, Handler] | None = None) -> Starlette:
    """The gateway's application: the table's routes, each with the handler given for it.

    A handler given for anything that is not an entry of the table is refused: the table is the
    only way a route comes to exist.
    """
    given = dict(handlers or {})
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
