"""The closed route table and the application built from it (`rhi_gateway.routes`, `rhi_gateway.app`).

The eight routes and the four upstream paths are written here from the system design (sections
4.1 and 6.2, step 1), and not read from the table. Requests are given to the application in the
scope the pinned server, uvicorn, builds from a request line: the path as it was sent in
`raw_path`, its decoded form in `path`. An HTTP client is not used, because it would normalize dot
segments away before the application saw them.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import unquote

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

from rhi_common.causes import Cause
from rhi_gateway import routes
from rhi_gateway.app import Handler, TableRoute, build_app

FORWARDED = {
    ("POST", "/v1/messages"),
    ("POST", "/v1/messages/count_tokens"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
}
LOCAL = {("GET", "/v1/models"), ("GET", "/status"), ("POST", "/wake"), ("HEAD", "/api/hello")}
BASE = "http://engine.example.com:8000"
UPSTREAM_URLS = {BASE + path for _, path in FORWARDED}
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

# The routes the issue names: the engine answers the first six, some of them without its key.
NAMED = ["/invocations", "/pause", "/tokenize", "/metrics", "/health", "/v1/completions", "/v1/messages%2F"]
# Other spellings of a path of the table, and paths that walk to or from one.
SPELLINGS = [
    "/v1/messages/",
    "/v1/models/",
    "/V1/MESSAGES",
    "/v1/Messages",
    "/Status",
    "/v1/%6dessages",
    "/v1/%6Dessages",
    "/v1%2Fmessages",
    "/v1/messages%2Fcount_tokens",
    "/v1/messages%3Fbeta=true",
    "/v1/./messages",
    "/v1/messages/.",
    "/v1/messages/../messages",
    "/v1/messages/../../pause",
    "/v1/%2e%2e/v1/messages",
    "/v1/messages/%2e%2e/%2e%2e/pause",
    "//v1/messages",
    "/v1//messages",
    "/v1/messages;x=1",
    "/v1",
    "/",
]
QUERIES = ["", "?beta=true", "?path=/v1/messages", "?/../pause", "?next=http://other.example.com/pause?x=1"]


def send(app: Starlette, method: str, target: str) -> tuple[int, bytes]:
    """The status and the body the application answers to one request line."""
    raw_path, _, query = target.encode("ascii").partition(b"?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "server": ("gateway.example.com", 80),
        "client": ("192.0.2.1", 50000),
        "method": method,
        "root_path": "",
        "path": unquote(raw_path.decode("ascii")),
        "raw_path": raw_path,
        "query_string": query,
        "headers": [],
        "state": {},
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def keep(message: Message) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, keep))
    return sent[0]["status"], b"".join(message.get("body", b"") for message in sent[1:])


def recording_app() -> tuple[Starlette, list[routes.Route]]:
    """An application whose every slot is filled, and the entries its handlers were called for."""
    called: list[routes.Route] = []

    def slot(entry: routes.Route) -> Handler:
        async def handler(request: Request, route: routes.Route) -> Response:
            assert route is entry
            called.append(entry)
            return Response(status_code=200)

        return handler

    return build_app({entry: slot(entry) for entry in routes.TABLE}), called


def assert_not_found(answer: tuple[int, bytes]) -> None:
    status, body = answer
    assert status == 404
    assert json.loads(body)["error"] == {"type": "not_found", "message": Cause.NOT_FOUND.sentence}


def test_the_table_is_the_designs() -> None:
    assert len(routes.TABLE) == 8
    by_kind = {kind: {(r.method, r.path) for r in routes.TABLE if r.kind is kind} for kind in routes.Kind}
    assert by_kind == {routes.Kind.FORWARDED: FORWARDED, routes.Kind.LOCAL: LOCAL}


def test_the_application_is_built_from_the_table() -> None:
    app, _ = recording_app()
    assert len(app.routes) == 8
    assert [route.entry for route in app.routes if isinstance(route, TableRoute)] == list(routes.TABLE)

    # Nothing but an entry of the table gets a slot.
    async def handler(request: Request, route: routes.Route) -> Response:
        return Response(status_code=200)

    with pytest.raises(ValueError, match="not in the table"):
        build_app({routes.Route("GET", "/pause"): handler})
    with pytest.raises(ValueError, match="not in the table"):
        build_app({routes.Route("GET", "/v1/messages"): handler})

    # A slot that nothing fills answers as a route outside the table does.
    empty = build_app()
    for method, path in FORWARDED | LOCAL:
        assert_not_found(send(empty, method, path))


@pytest.mark.parametrize(("method", "path"), sorted(FORWARDED | LOCAL))
def test_the_eight_entries_answer(method: str, path: str) -> None:
    app, called = recording_app()
    # With and without a query string: Claude Code sends `POST /v1/messages?beta=true`.
    for query in QUERIES:
        assert send(app, method, path + query)[0] == 200
    assert [(entry.method, entry.path) for entry in called] == [(method, path)] * len(QUERIES)

    # The path under any other method is not a route: 404, not 405.
    for other in set(METHODS) - {method} - {m for m, p in FORWARDED | LOCAL if p == path}:
        assert_not_found(send(app, other, path))
        assert_not_found(send(app, other.lower(), path))
    assert len(called) == len(QUERIES)


@pytest.mark.parametrize("path", NAMED + SPELLINGS)
def test_everything_else_is_not_found(path: str) -> None:
    app, called = recording_app()
    for method in METHODS:
        for query in QUERIES:
            assert_not_found(send(app, method, path + query))
    assert called == []


def test_the_upstream_url_is_the_tables() -> None:
    built: list[str] = []

    async def forward(request: Request, route: routes.Route) -> Response:
        built.append(routes.upstream_url(BASE, route))
        return Response(status_code=200)

    app = build_app({entry: forward for entry in routes.TABLE if entry.kind is routes.Kind.FORWARDED})

    # Dot segments and encoded characters: none of them is forwarded anywhere.
    for path in NAMED + SPELLINGS:
        for query in QUERIES:
            assert_not_found(send(app, "POST", path + query))
    assert built == []

    # Query strings on the four forwarded routes: the request is forwarded, and its query is not.
    for _, path in sorted(FORWARDED):
        for query in QUERIES:
            assert send(app, "POST", path + query)[0] == 200
            assert built[-1] == BASE + path
    assert len(built) == len(FORWARDED) * len(QUERIES)
    assert set(built) == UPSTREAM_URLS

    # The URL is the base and the entry's constant, whatever the base ends with.
    assert {routes.upstream_url(BASE + "/", entry) for entry in routes.TABLE[:4]} == UPSTREAM_URLS
    # A route the gateway answers itself has no upstream URL.
    for entry in routes.TABLE:
        if entry.kind is routes.Kind.LOCAL:
            with pytest.raises(ValueError, match="no upstream URL"):
                routes.upstream_url(BASE, entry)
