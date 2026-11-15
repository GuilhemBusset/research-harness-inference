"""Independent checks of the closed gateway table on the examined commit."""

import asyncio
import json
from urllib.parse import unquote

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message, Scope

from rhi_gateway.app import build_app
from rhi_gateway.routes import TABLE, Kind, Route, upstream_url

FORWARDED = {
    ("POST", "/v1/messages"),
    ("POST", "/v1/messages/count_tokens"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/responses"),
}
LOCAL = {("GET", "/v1/models"), ("GET", "/status"), ("POST", "/wake"), ("HEAD", "/api/hello")}
BASE = "http://engine.example.com:8000"


def exchange(app: Starlette, method: str, target: str) -> tuple[int, bytes]:
    """Give ASGI the original path, without a client's dot-segment normalization."""
    raw, _, query = target.encode("ascii").partition(b"?")
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "root_path": "",
        "path": unquote(raw.decode("ascii")),
        "raw_path": raw,
        "query_string": query,
        "headers": [],
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent[0]["status"], b"".join(message.get("body", b"") for message in sent[1:])


def refused(answer: tuple[int, bytes]) -> None:
    status, body = answer
    assert status == 404
    assert json.loads(body)["error"]["type"] == "not_found"


def test_exact_table_and_separate_slots() -> None:
    assert len(TABLE) == 8
    assert {(r.method, r.path) for r in TABLE if r.kind == Kind.FORWARDED} == FORWARDED
    assert {(r.method, r.path) for r in TABLE if r.kind == Kind.LOCAL} == LOCAL
    seen: list[Route] = []

    async def slot(request: Request, entry: Route) -> Response:
        seen.append(entry)
        return Response(content=entry.method + " " + entry.path, status_code=202)

    app = build_app(dict.fromkeys(TABLE, slot))
    assert isinstance(app, Starlette)
    assert len(app.routes) == 8
    for method, path in FORWARDED | LOCAL:
        assert exchange(app, method, path) == (202, f"{method} {path}".encode())
        assert (seen[-1].method, seen[-1].path) == (method, path)
        refused(exchange(build_app(), method, path))
    assert len(seen) == 8
    with pytest.raises(ValueError):
        build_app({Route("GET", "/health"): slot})


def test_refusals_never_call_a_handler() -> None:
    called: list[Route] = []

    async def slot(request: Request, entry: Route) -> Response:
        called.append(entry)
        return Response(status_code=200)

    app = build_app(dict.fromkeys(TABLE, slot))
    denied = {"/invocations", "/pause", "/tokenize", "/metrics", "/health", "/v1/completions"}
    for _, path in FORWARDED | LOCAL:
        denied.update((path + "/", path.upper(), path + "%2F", "/x/.." + path))
        for index, char in enumerate(path):
            denied.add(path[:index] + f"%{ord(char):02x}" + path[index + 1:])
    methods = ("GET", "HEAD", "POST", "OPTIONS", "PATCH", "PUT", "DELETE", "TRACE", "CONNECT", "post")
    for query in ("", "?beta=true", "?next=/v1/messages&path=/../pause"):
        for method in methods:
            for path in denied:
                refused(exchange(app, method, path + query))
            for allowed_method, path in FORWARDED | LOCAL:
                if method != allowed_method:
                    refused(exchange(app, method, path + query))
    assert called == []


def test_forwarding_uses_constant_urls() -> None:
    urls: list[str] = []

    async def slot(request: Request, entry: Route) -> Response:
        urls.append(upstream_url(BASE + "/", entry))
        return Response(status_code=204)

    app = build_app({r: slot for r in TABLE if r.kind == Kind.FORWARDED})
    for target in (
        "/v1/./messages?beta=true",
        "/v1/messages/../responses",
        "/v1/%6dessages?x=1",
        "/v1/messages%2f?path=/pause",
        "/v1/messages%3fnext=/pause",
        "/v1/%2e%2e/responses",
    ):
        refused(exchange(app, "POST", target))
    assert urls == []
    for _, path in FORWARDED:
        for query in ("", "?beta=true", "?url=https://example.com/pause&path=/../metrics"):
            assert exchange(app, "POST", path + query)[0] == 204
            assert urls[-1] == BASE + path
    assert len(urls) == 12
    assert set(urls) == {BASE + path for _, path in FORWARDED}
    for entry in TABLE:
        if entry.kind == Kind.LOCAL:
            with pytest.raises(ValueError):
                upstream_url(BASE, entry)
