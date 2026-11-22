"""The warm path of a forwarded request (`rhi_gateway.proxy`, `rhi_gateway.app.forwarding_handlers`).

The gateway runs whole, over the in-memory key store and ledger. The upstream is an
`httpx.MockTransport` written here: it records every request and answers with canned streams in
the three wire formats. The student's key is generated when the tests run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.types import Message

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.students import StudentKey, utc_day
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.readiness import PoolView

NOW = 1_800_000_000
STUDENT = 4242
UPSTREAM = "http://pools.example.com:8080"
SECRETS = {"coursework": "coursework-secret", "upgrade": "upgrade-secret"}
ELSEWHERE = "other.example.com"

MOCK = {
    "engine": "mock",
    "instance_types": ["t3.small"],
    "prices": {"t3.small": 0.0208},
    "max_instances": 1,
    "warm_instances": 0,
    "wake": "on-demand",
    "target_concurrency": 8,
    "context_length": 131072,
    "largest_output": 8000,
}
POOLS = parse_pools({"coursework": MOCK, "upgrade": MOCK})


def sse(*events: tuple[str | None, dict[str, Any] | str]) -> bytes:
    lines = b""
    for name, data in events:
        text = data if isinstance(data, str) else json.dumps(data)
        lines += (f"event: {name}\n" if name else "").encode() + f"data: {text}\n\n".encode()
    return lines


MESSAGES_STREAM = sse(
    ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 21, "output_tokens": 1}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
    ("message_stop", {"type": "message_stop"}),
)
CHAT_USAGE = sse((None, {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 9}}))
CHAT_STREAM_BEFORE = sse((None, {"choices": [{"index": 0, "delta": {"content": "Hi"}}], "usage": None}))
CHAT_STREAM = CHAT_STREAM_BEFORE + CHAT_USAGE + sse((None, "[DONE]"))
RESPONSES_STREAM = sse(
    ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Hi"}),
    (
        "response.completed",
        {"type": "response.completed", "response": {"usage": {"input_tokens": 40, "output_tokens": 11}}},
    ),
)
COUNT_ANSWER = b'{"input_tokens":17}'

# Each forwarded route: a request body and the upstream's answer, a stream or a token count.
CASES: dict[routes.Route, tuple[dict[str, Any], bytes]] = {
    routes.MESSAGES: ({"model": "coursework", "max_tokens": 100, "stream": True, "messages": []}, MESSAGES_STREAM),
    routes.CHAT_COMPLETIONS: (
        {"model": "coursework", "stream": True, "stream_options": {"include_usage": True}, "messages": []},
        CHAT_STREAM,
    ),
    routes.RESPONSES: ({"model": "coursework", "stream": True, "input": "hello"}, RESPONSES_STREAM),
    routes.COUNT_TOKENS: ({"model": "coursework", "messages": []}, COUNT_ANSWER),
}
REPORTED = {routes.MESSAGES: 28, routes.CHAT_COMPLETIONS: 39, routes.RESPONSES: 51, routes.COUNT_TOKENS: 17}


def split(data: bytes, size: int = 7) -> list[bytes]:
    """The answer cut into chunks that fall in the middle of lines and events."""
    return [data[at : at + size] for at in range(0, len(data), size)]


class Upstream:
    """The pools' load balancer, in the test: it records each request and answers the path's canned bytes."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.seen: list[httpx.Request] = []
        self.gate = gate

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.seen.append(request)
        entry = next(entry for entry in CASES if entry.upstream_path == request.url.path)
        answer = CASES[entry][1]
        if entry == routes.COUNT_TOKENS:
            return httpx.Response(200, headers={"content-type": "application/json"}, content=answer)

        async def chunks() -> AsyncIterator[bytes]:
            pieces = split(answer)
            yield pieces[0]
            if self.gate is not None:
                await self.gate.wait()
            for piece in pieces[1:]:
                yield piece

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())


def gateway(upstream: Upstream, store: InMemoryKeyStore | None = None) -> tuple[Starlette, str, InMemoryLedger]:
    """The gateway of one student, whose key item is put in `store` (a new store when none is given)."""
    key = generate_key()
    item = StudentKey(
        key_hash=key_hash(key),
        github_id=STUDENT,
        alias="otter",
        installation=None,
        issued_at=NOW - 3600,
        expires_at=NOW + 90 * 86400,
        revoked_at=None,
        daily_token_budget=2_000_000,
        concurrency_limit=4,
    )
    store = InMemoryKeyStore() if store is None else store
    store.put(item)
    ledger = InMemoryLedger(clock=lambda: float(NOW))
    filters = {
        routes.MESSAGES: BodyFilter(messages.MESSAGES, POOLS),
        routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, POOLS),
        routes.CHAT_COMPLETIONS: ChatBodyFilter(POOLS),
        routes.RESPONSES: BodyFilter(responses.RESPONSES, POOLS),
    }
    client = proxy.upstream_client(httpx.MockTransport(upstream))
    forwarding = Forwarding(
        authenticator=Authenticator(store, wall_clock=lambda: float(NOW)),
        ledger=ledger,
        filters=filters,
        proxy=proxy.Proxy(client, UPSTREAM, SECRETS),
        # Every pool is ready: these requests are forwarded at once, with no hold.
        holding=Holding(views=lambda: {pool.name: PoolView(ready=True) for pool in POOLS}, wake=lambda pool: None),
    )
    return build_app(forwarding_handlers(forwarding)), key, ledger


def call(app: Starlette, sends: Callable[[httpx.AsyncClient], Any]) -> Any:
    async def run() -> Any:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example.com") as client:
            return await sends(client)

    return asyncio.run(run())


@pytest.mark.parametrize("entry", list(CASES), ids=lambda entry: entry.path)
def test_an_answer_reaches_the_client_whole_and_its_usage_is_settled(entry: routes.Route) -> None:
    upstream = Upstream()
    app, key, ledger = gateway(upstream)
    body, answer = CASES[entry]

    response = call(app, lambda client: client.post(entry.path, json=body, headers={"x-api-key": key}))

    assert response.status_code == 200
    assert response.content == answer
    day = ledger.day(STUDENT, utc_day(NOW))
    assert day is not None
    assert (day.tokens, day.leases) == (REPORTED[entry], {})
    assert len(upstream.seen) == 1
    sent = upstream.seen[0]
    assert sent.headers["authorization"] == "Bearer " + SECRETS["coursework"]
    assert all(key not in value for _, value in sent.headers.multi_items())
    assert key.encode() not in sent.content


def test_a_chat_client_that_did_not_ask_for_usage_is_settled_without_seeing_it() -> None:
    upstream = Upstream()
    app, key, ledger = gateway(upstream)
    body = {"model": "coursework", "stream": True, "messages": []}

    response = call(
        app, lambda client: client.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer " + key})
    )

    assert response.content == CHAT_STREAM_BEFORE + sse((None, "[DONE]"))
    assert json.loads(upstream.seen[0].content)["stream_options"] == {"include_usage": True}
    day = ledger.day(STUDENT, utc_day(NOW))
    assert day is not None
    assert (day.tokens, day.leases) == (39, {})


@pytest.mark.parametrize("erased", [False, True])
def test_a_key_revoked_or_erased_while_the_body_arrives_keeps_nothing(monkeypatch: pytest.MonkeyPatch, erased: bool) -> None:
    upstream, store = Upstream(), InMemoryKeyStore()
    app, key, ledger = gateway(upstream, store)

    async def body() -> AsyncIterator[bytes]:
        # The key was accepted from the headers. Before the body comes it is revoked, or its
        # student erased, which deletes it: the reservation's write may come after either.
        store.revoke(key_hash(key), at=NOW)
        if erased:
            monkeypatch.setattr(store, "lookup", lambda _: None)
        yield json.dumps(CASES[routes.MESSAGES][0]).encode()

    response = call(app, lambda client: client.post("/v1/messages", content=body(), headers={"x-api-key": key}))

    assert (response.status_code, upstream.seen) == (401, [])
    day = ledger.day(STUDENT, utc_day(NOW))
    # Revoked: the reservation is taken back, nothing charged. Erased: the usage row is deleted.
    assert (day is None) if erased else (day is not None and (day.tokens, day.leases) == (0, {}))


def test_each_chunk_reaches_the_client_before_the_next_one_is_sent() -> None:
    async def run() -> list[bytes]:
        gate = asyncio.Event()
        app, key, _ = gateway(Upstream(gate))
        body = json.dumps(CASES[routes.MESSAGES][0]).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/messages",
            "raw_path": b"/v1/messages",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"x-api-key", key.encode()), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("gateway.example.com", 443),
        }
        received: list[bytes] = []
        sent = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive() -> Message:
            if sent:
                return sent.pop()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def send(message: Message) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                received.append(message["body"])
                # The upstream sends the rest only once the client has its first chunk.
                gate.set()

        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        return received

    received = asyncio.run(run())

    assert received[0] == split(MESSAGES_STREAM)[0]
    assert b"".join(received) == MESSAGES_STREAM


def test_the_timeouts_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    given: list[dict[str, Any]] = []

    class Recording(httpx.AsyncClient):
        def __init__(self, **arguments: Any) -> None:
            given.append(arguments)
            super().__init__(**arguments)

    monkeypatch.setattr(httpx, "AsyncClient", Recording)
    client = proxy.upstream_client()

    timeout = client.timeout
    assert (timeout.connect, timeout.write, timeout.read, timeout.pool) == (10, 60, 1200, 10)
    transport = client._transport
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    assert vars(transport._pool)["_keepalive_expiry"] == 5
    # Passed by the gateway, not taken from the library's defaults.
    assert given[0]["timeout"] == httpx.Timeout(connect=10, write=60, read=1200, pool=10)
    assert given[0]["limits"].keepalive_expiry == 5


def test_the_upstream_is_the_one_given() -> None:
    upstream = Upstream()
    app, key, _ = gateway(upstream)
    elsewhere = f"http://{ELSEWHERE}/v1/messages"
    headers = {
        "x-api-key": key,
        "Host": ELSEWHERE,
        "X-Forwarded-Host": ELSEWHERE,
        "Forwarded": f"host={ELSEWHERE}",
        "Referer": elsewhere,
    }
    chosen = []
    requests = []
    for entry, (body, _) in CASES.items():
        for model, claimed in (("coursework", "upgrade"), ("upgrade", "coursework")):
            named = {**body, "model": model, "base_url": elsewhere, "url": elsewhere, "metadata": {"host": ELSEWHERE}}
            path = f"{entry.path}?host={ELSEWHERE}&url={elsewhere}"
            requests.append((path, named, {**headers, "X-Rhi-Pool": claimed}))
            chosen.append((entry.upstream_path, model))

    async def sends(client: httpx.AsyncClient) -> list[httpx.Response]:
        return [await client.post(path, json=body, headers=sent) for path, body, sent in requests]

    answers = call(app, sends)

    assert [answer.status_code for answer in answers] == [200] * len(requests)
    assert len(upstream.seen) == len(requests)
    for sent, (path, pool) in zip(upstream.seen, chosen, strict=True):
        assert str(sent.url) == UPSTREAM + str(path)
        assert sent.headers["host"] == "pools.example.com:8080"
        assert sent.headers.get_list("x-rhi-pool") == [pool]
        assert sent.headers["authorization"] == "Bearer " + SECRETS[pool]
        assert ELSEWHERE.encode() not in sent.content
