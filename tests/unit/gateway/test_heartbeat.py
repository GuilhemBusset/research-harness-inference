"""This task's count of requests in flight (`rhi_gateway.heartbeat`; system design, sections 5.3 and 7.2).

The count has no table: it is read through `Heartbeat.rows`, as the gateway's gauge reads it. The
gateway runs whole, with the in-memory key store and ledger and a real `Readiness` whose AWS
clients are doubles. The engine is an `httpx.MockTransport` handler that answers when the test
lets it. Every key is generated when the tests run.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from starlette.types import Message

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.records import SESSION_RECORD, GatewayHeartbeat, Session
from rhi_common.students import StudentKey
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, Pools, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.heartbeat import Heartbeat
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.readiness import Readiness

NOW = 1_800_000_000
# The course end the ledger's rows expire by.
COURSE_END_DATE = datetime.date(2027, 6, 30)
TASK = "0123456789abcdef"
POOLS = parse_pools(
    {
        "coursework": {
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
    }
)
POOL = POOLS[0].name
STREAM = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
ANSWER = json.dumps({"type": "message", "usage": {"input_tokens": 3, "output_tokens": 2}}).encode()


class AWS:
    """`describe_target_health` (healthy once `healthy`) and the pool-state table.

    The table holds one record, the pool's session, active for 6 hours from the moment the test's clock starts at.
    """

    def __init__(self) -> None:
        self.healthy = False

    def describe_target_health(self, TargetGroupArn: str) -> dict[str, Any]:
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy" if self.healthy else "initial"}}]}

    def get_item(self, Key: dict[str, str]) -> dict[str, Any]:
        if Key["record"] != SESSION_RECORD:
            return {}
        session = Session(Key["pool"], instances=1, starts_at=NOW, ends_at=NOW + 6 * 3600, set_by="owner", set_at=NOW)
        return {"Item": session.to_item()}


async def until(condition: Callable[[], bool]) -> None:
    """Wait for what a worker thread or another task does. A guard, not the clock under test."""

    async def polled() -> None:
        while not condition():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(polled(), timeout=10)


def counts(row: GatewayHeartbeat) -> tuple[int, int]:
    """The requests in flight, and those held: none ever is."""
    return row.in_flight, row.held


class Engine:
    """The pool's engine: records the heartbeat's row when reached, then answers once `release` is set."""

    def __init__(self, heartbeat: Heartbeat) -> None:
        self._heartbeat = heartbeat
        self.release = asyncio.Event()
        self.seen: list[tuple[int, int]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        self.seen.append(counts(self._heartbeat.rows()[0]))
        await self.release.wait()
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 3})
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=STREAM)
        return httpx.Response(200, headers={"content-type": "application/json"}, content=ANSWER)


class Gateway:
    """The whole gateway, local routes included, with one count; its pool is not ready until the test says so."""

    def __init__(self) -> None:
        self.aws = AWS()
        self.readiness = Readiness(
            {POOL: "coursework-targets"}, elbv2=self.aws, table=self.aws, clock=lambda: float(NOW)
        )
        self.readiness.refresh()
        self.heartbeat = Heartbeat(TASK, [POOL], clock=lambda: float(NOW))
        self.engine = Engine(self.heartbeat)
        self.key = generate_key()
        student = StudentKey(
            key_hash=key_hash(self.key),
            github_id=4242,
            alias="otter",
            installation=None,
            issued_at=NOW - 3600,
            expires_at=NOW + 86400,
            revoked_at=None,
            concurrency_limit=4,
        )
        authenticator = Authenticator(InMemoryKeyStore(student), wall_clock=lambda: float(NOW))
        ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=lambda: float(NOW))
        forwarding = Forwarding(
            authenticator=authenticator,
            ledger=ledger,
            filters={
                routes.MESSAGES: BodyFilter(messages.MESSAGES, POOLS),
                routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, POOLS),
                routes.CHAT_COMPLETIONS: ChatBodyFilter(POOLS),
                routes.RESPONSES: BodyFilter(responses.RESPONSES, POOLS),
            },
            proxy=proxy.Proxy(
                proxy.upstream_client(httpx.MockTransport(self.engine)),
                "http://pools.example.com:8080",
                {POOL: "secret"},
            ),
            pools=Pools(views=self.readiness.views, clock=lambda: float(NOW)),
            heartbeat=self.heartbeat,
        )
        local = LocalRoutes(
            authenticator=authenticator, pools=POOLS, views=self.readiness.views, clock=lambda: float(NOW)
        )
        self.app = build_app(forwarding_handlers(forwarding), local=local, heartbeat=self.heartbeat)

    def ready(self) -> None:
        self.aws.healthy = True
        self.readiness.refresh()

    def row(self) -> tuple[int, int]:
        return counts(self.heartbeat.rows()[0])


class Call:
    """One client's request over ASGI; the client stays until it has its whole answer or the test makes it leave."""

    def __init__(self, gateway: Gateway, entry: routes.Route, body: dict[str, Any] | None = None) -> None:
        payload = b"" if body is None else json.dumps(body).encode()
        self._request: list[Message] = [{"type": "http.request", "body": payload, "more_body": False}]
        self._left = asyncio.Event()
        self.status: int | None = None
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": entry.method,
            "scheme": "https",
            "path": entry.path,
            "raw_path": entry.path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"x-api-key", gateway.key.encode()), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("gateway.example.com", 443),
        }
        self.task = asyncio.ensure_future(gateway.app(scope, self._receive, self._send))

    async def _receive(self) -> Message:
        if self._request:
            return self._request.pop()
        await self._left.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]

    def leave(self) -> None:
        self._left.set()

    async def ended(self) -> None:
        await asyncio.wait_for(self.task, timeout=10)


def test_a_request_counts_until_it_ends() -> None:
    heartbeat = Heartbeat(TASK, [POOL, "other"], clock=lambda: float(NOW))
    first, second = heartbeat.received(POOL), heartbeat.received(POOL)
    assert [(row.task, row.pool, *counts(row)) for row in heartbeat.rows()] == [
        (TASK, POOL, 2, 0),
        (TASK, "other", 0, 0),
    ]
    first.end()
    first.end()
    assert counts(heartbeat.rows()[0]) == (1, 0)
    second.end()
    assert counts(heartbeat.rows()[0]) == (0, 0)


def test_cheap_calls_are_not_counted() -> None:
    async def scenario() -> None:
        gateway = Gateway()
        gateway.ready()
        gateway.engine.release.set()
        calls = [
            Call(gateway, routes.STATUS),
            Call(gateway, routes.MODELS),
            Call(gateway, routes.HELLO),
            Call(gateway, routes.COUNT_TOKENS, {"model": POOL, "messages": []}),
        ]
        for call in calls:
            await call.ended()
        assert [call.status for call in calls] == [200, 200, 200, 200]
        assert gateway.engine.seen == [(0, 0)]  # Token counting, while it was with the engine.
        assert gateway.row() == (0, 0)

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "not a stream"])
def test_a_request_is_in_flight_until_its_end(stream: bool) -> None:
    async def scenario() -> None:
        gateway = Gateway()
        gateway.ready()
        call = Call(gateway, routes.MESSAGES, {"model": POOL, "max_tokens": 10, "messages": [], "stream": stream})
        await until(lambda: bool(gateway.engine.seen))
        assert gateway.engine.seen == [(1, 0)]

        gateway.engine.release.set()
        await call.ended()
        assert (call.status, gateway.row()) == (200, (0, 0))

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "not a stream"])
def test_a_request_the_pool_cannot_serve_is_never_counted(stream: bool) -> None:
    async def scenario() -> None:
        gateway = Gateway()  # Its session is active, and no target is healthy.
        call = Call(gateway, routes.MESSAGES, {"model": POOL, "max_tokens": 10, "messages": [], "stream": stream})
        await call.ended()
        assert (call.status, gateway.engine.seen, gateway.row()) == (503, [], (0, 0))

    asyncio.run(scenario())


def test_a_request_the_client_leaves_stops_counting() -> None:
    async def scenario() -> None:
        gateway = Gateway()
        gateway.ready()
        call = Call(gateway, routes.MESSAGES, {"model": POOL, "max_tokens": 10, "messages": []})
        await until(lambda: gateway.row() == (1, 0))
        call.leave()
        await call.ended()
        assert gateway.row() == (0, 0)

    asyncio.run(scenario())
