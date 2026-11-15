"""Independent behavioural checks for issue 60's closed gateway surface."""

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
METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "PROPFIND", "post")
QUERIES = (b"", b"beta=true", b"next=/../pause&url=https://example.com/metrics")


def request(app: Starlette, method: str, path: str, query: bytes = b"") -> tuple[int, bytes]:
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "root_path": "",
        "path": unquote(path),
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "headers": [],
        "server": ("example.com", 80),
        "client": ("192.0.2.2", 50000),
    }
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    assert messages[0]["type"] == "http.response.start"
    return messages[0]["status"], b"".join(m.get("body", b"") for m in messages[1:])


def assert_refused(answer: tuple[int, bytes]) -> None:
    status, body = answer
    assert status == 404
    assert json.loads(body)["error"]["type"] == "not_found"


def filled_app() -> tuple[Starlette, list[tuple[str, str]]]:
    seen: list[tuple[str, str]] = []

    async def handler(req: Request, entry: Route) -> Response:
        seen.append((entry.method, entry.path))
        return Response(content=entry.method + " " + entry.path, status_code=202)

    return build_app(dict.fromkeys(TABLE, handler)), seen


def test_table_and_all_handler_slots() -> None:
    assert len(TABLE) == 8
    assert {(r.method, r.path) for r in TABLE if r.kind == Kind.FORWARDED} == FORWARDED
    assert {(r.method, r.path) for r in TABLE if r.kind == Kind.LOCAL} == LOCAL
    assert {(r.method, r.path, r.upstream_path) for r in TABLE if r.kind == Kind.FORWARDED} == {
        (method, path, path) for method, path in FORWARDED
    }
    app, seen = filled_app()
    assert isinstance(app, Starlette)
    assert len(app.routes) == 8
    for method, path in sorted(FORWARDED | LOCAL):
        for query in QUERIES:
            assert request(app, method, path, query) == (202, (method + " " + path).encode())
            assert seen[-1] == (method, path)
    assert len(seen) == 8 * len(QUERIES)

    async def outside(req: Request, entry: Route) -> Response:
        return Response(status_code=202)

    with pytest.raises(ValueError):
        build_app({Route("POST", "/invocations"): outside})


def test_wrong_methods_are_not_routes() -> None:
    app, seen = filled_app()
    for expected_method, path in FORWARDED | LOCAL:
        for method in METHODS:
            if method != expected_method:
                for query in QUERIES:
                    assert_refused(request(app, method, path, query))
    assert seen == []


def test_forbidden_routes_and_encoded_spellings() -> None:
    refused = {"/invocations", "/pause", "/tokenize", "/metrics", "/health", "/v1/completions"}
    for _, path in FORWARDED | LOCAL:
        refused.update((path + "/", path.upper(), "/" + path, path + "/../messages"))
        for i, character in enumerate(path):
            refused.add(path[:i] + f"%{ord(character):02X}" + path[i + 1:])
    app, seen = filled_app()
    for path in refused:
        for method in METHODS:
            for query in QUERIES:
                assert_refused(request(app, method, path, query))
    assert seen == []


def test_upstream_urls_are_constants_for_adversarial_targets() -> None:
    seen: list[str] = []

    async def forward(req: Request, entry: Route) -> Response:
        url = upstream_url(BASE + "/", entry)
        seen.append(url)
        return Response(content=url)

    app = build_app({r: forward for r in TABLE if r.kind == Kind.FORWARDED})
    for path in (
        "/v1/./messages",
        "/v1/messages/../../pause",
        "/v1/messages%2f",
        "/v1/%6dessages",
        "/v1/messages/%2e%2e/responses",
        "/v1/messages%3fnext=/pause",
    ):
        for query in QUERIES:
            assert_refused(request(app, "POST", path, query))
    assert seen == []
    for method, path in FORWARDED:
        for query in QUERIES:
            assert request(app, method, path, query) == (200, (BASE + path).encode())
    assert set(seen) == {BASE + path for _, path in FORWARDED}
    assert len(seen) == 4 * len(QUERIES)
    for entry in TABLE:
        if entry.kind == Kind.LOCAL:
            with pytest.raises(ValueError):
                upstream_url(BASE, entry)
