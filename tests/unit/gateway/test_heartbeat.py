"""The demand a gateway task reports to the scaler (`rhi_gateway.heartbeat`; system design, section 5.1).

The heartbeat writes to a table double that keeps every item it is given. Its clock and its sleep
are the test's. The gateway runs whole, with the in-memory key store and ledger and a real
`Readiness` and `Waker` whose AWS clients are doubles. The engine is an `httpx.MockTransport`
handler that answers when the test lets it. Every key is generated when the tests run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from starlette.types import Message

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.records import GatewayHeartbeat
from rhi_common.students import StudentKey
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.heartbeat import Heartbeat
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.readiness import Readiness
from rhi_gateway.waker import EXPLICIT_WAKE_SECONDS, Waker

NOW = 1_800_000_000
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


class Clock:
    """Seconds since the epoch, moved only by the test; `sleep` waits on it."""

    def __init__(self) -> None:
        self.now = float(NOW)
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        wake = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, wake))
        await wake

    async def advance(self, seconds: float) -> None:
        """Move the clock and wake every sleeper whose time has come."""
        self.now += seconds
        while due := [wake for at, wake in self._sleepers if at <= self.now and not wake.done()]:
            for wake in due:
                wake.set_result(None)
            for _ in range(50):
                await asyncio.sleep(0)


class Table:
    """The heartbeats table: every item put, in order."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put_item(self, Item: dict[str, Any]) -> None:
        self.items.append(Item)

    @property
    def rows(self) -> list[GatewayHeartbeat]:
        return [GatewayHeartbeat.from_item(item) for item in self.items]


class AWS:
    """`describe_target_health` (healthy once `healthy`), the pool-state table (no records) and the scaler's `invoke`."""

    def __init__(self) -> None:
        self.healthy = False

    def describe_target_health(self, TargetGroupArn: str) -> dict[str, Any]:
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy" if self.healthy else "initial"}}]}

    def get_item(self, Key: dict[str, str]) -> dict[str, Any]:
        return {}

    def invoke(self, **_: Any) -> None:
        return None


async def until(condition: Callable[[], bool]) -> None:
    """Wait for what a worker thread or another task does. A guard, not the clock under test."""

    async def polled() -> None:
        while not condition():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(polled(), timeout=10)


def counts(row: GatewayHeartbeat) -> tuple[int, int, int | None]:
    return row.in_flight, row.held, row.last_request_at


class Engine:
    """The pool's engine: records the heartbeat's row when reached, then answers once `release` is set."""

    def __init__(self, heartbeat: Heartbeat) -> None:
        self._heartbeat = heartbeat
        self.release = asyncio.Event()
        self.seen: list[tuple[int, int, int | None]] = []

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
    """The whole gateway, local routes included, with one heartbeat; its pool is not ready until the test says so."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.aws = AWS()
        self.readiness = Readiness({POOL: "coursework-targets"}, elbv2=self.aws, table=self.aws, clock=self.clock.time)
        self.readiness.refresh()
        self.waker = Waker(self.readiness, lambda_client=self.aws, function_name="scaler", clock=self.clock.time)
        self.table = Table()
        self.heartbeat = Heartbeat(
            self.table, TASK, [POOL], waker=self.waker, clock=self.clock.time, sleep=self.clock.sleep
        )
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
            daily_token_budget=2_000_000,
            concurrency_limit=4,
        )
        authenticator = Authenticator(InMemoryKeyStore(student), wall_clock=lambda: float(NOW))
        ledger = InMemoryLedger(clock=lambda: float(NOW))
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
                sleep=self.clock.sleep,
            ),
            holding=Holding(
                views=self.readiness.views, wake=lambda pool: None, clock=self.clock.time, sleep=self.clock.sleep
            ),
            heartbeat=self.heartbeat,
        )
        local = LocalRoutes(authenticator=authenticator, pools=POOLS, views=self.readiness.views, clock=self.clock.time)
        self.app = build_app(forwarding_handlers(forwarding), local=local, heartbeat=self.heartbeat)

    def ready(self) -> None:
        self.aws.healthy = True
        self.readiness.refresh()

    def row(self) -> tuple[int, int, int | None]:
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


def test_two_in_flight_and_one_held_are_rewritten_every_five_seconds() -> None:
    async def scenario() -> None:
        clock, table = Clock(), Table()
        heartbeat = Heartbeat(table, TASK, [POOL, "other"], clock=clock.time, sleep=clock.sleep)
        heartbeat.received(POOL, held=False)
        clock.now += 1
        heartbeat.received(POOL, held=False)
        clock.now += 1
        heartbeat.received(POOL, held=True)
        writing = asyncio.ensure_future(heartbeat.run())

        await until(lambda: len(table.items) == 2)
        await clock.advance(4.5)
        await asyncio.sleep(0.01)
        assert len(table.items) == 2  # Nothing before the 5 seconds are up.
        await clock.advance(0.5)
        await until(lambda: len(table.items) == 4)
        await clock.advance(5)
        await until(lambda: len(table.items) == 6)
        writing.cancel()

        rows = [row for row in table.rows if row.pool == POOL]
        assert {item["source"] for item in table.items} == {"gateway#" + TASK}
        assert [counts(row) for row in rows] == [(2, 1, NOW + 2)] * 3
        assert [row.written_at for row in rows] == [NOW + 2, NOW + 7, NOW + 12]
        assert {counts(row) for row in table.rows if row.pool == "other"} == {(0, 0, None)}

    asyncio.run(scenario())


def test_a_request_counts_until_it_ends() -> None:
    heartbeat = Heartbeat(Table(), TASK, [POOL], clock=lambda: float(NOW))
    demand = heartbeat.received(POOL, held=True)
    demand.forwarded()
    assert counts(heartbeat.rows()[0]) == (1, 0, NOW)
    demand.end()
    demand.end()
    demand.forwarded()
    assert counts(heartbeat.rows()[0]) == (0, 0, NOW)


def test_cheap_calls_are_not_demand() -> None:
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
        assert gateway.engine.seen == [(0, 0, None)]  # Token counting, while it was with the engine.
        assert gateway.row() == (0, 0, None)

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "not a stream"])
def test_a_request_is_held_then_in_flight_until_its_end(stream: bool) -> None:
    async def scenario() -> None:
        gateway = Gateway()
        call = Call(gateway, routes.MESSAGES, {"model": POOL, "max_tokens": 10, "messages": [], "stream": stream})
        await until(lambda: gateway.row()[1] == 1)
        assert gateway.row() == (0, 1, NOW)

        await gateway.clock.advance(3)
        gateway.ready()
        await gateway.clock.advance(1)
        await until(lambda: bool(gateway.engine.seen))
        assert gateway.engine.seen == [(1, 0, NOW)]

        gateway.engine.release.set()
        await call.ended()
        assert gateway.row() == (0, 0, NOW)

    asyncio.run(scenario())


def test_a_hold_the_client_leaves_stops_counting() -> None:
    async def scenario() -> None:
        gateway = Gateway()
        call = Call(gateway, routes.MESSAGES, {"model": POOL, "max_tokens": 10, "messages": []})
        await until(lambda: gateway.row()[1] == 1)
        call.leave()
        await call.ended()
        assert gateway.row() == (0, 0, NOW)

    asyncio.run(scenario())


def test_an_explicit_wake_is_demand() -> None:
    gateway = Gateway()
    gateway.waker.record(POOL)
    assert gateway.row() == (0, 1, None)

    gateway.clock.now += 40
    gateway.ready()
    assert gateway.row() == (0, 0, NOW + 40)


def test_an_explicit_wake_that_runs_out_records_nothing() -> None:
    gateway = Gateway()
    gateway.waker.record(POOL)
    gateway.clock.now += EXPLICIT_WAKE_SECONDS
    assert gateway.row() == (0, 0, None)
    gateway.ready()
    assert gateway.row() == (0, 0, None)


def test_shutdown_writes_a_row_of_zeros() -> None:
    async def scenario() -> None:
        table = Table()
        heartbeat = Heartbeat(table, TASK, [POOL], clock=lambda: float(NOW))
        heartbeat.received(POOL, held=False)
        events: asyncio.Queue[Message] = asyncio.Queue()
        sent: list[str] = []

        async def send(message: Message) -> None:
            sent.append(message["type"])

        await events.put({"type": "lifespan.startup"})
        app = build_app(heartbeat=heartbeat)
        running = asyncio.ensure_future(app({"type": "lifespan", "asgi": {"version": "3.0"}}, events.get, send))
        await until(lambda: bool(table.items))  # Started with the application.
        await events.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(running, timeout=10)
        assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
        assert counts(table.rows[0]) == (1, 0, NOW)
        assert counts(table.rows[-1]) == (0, 0, NOW)

    asyncio.run(scenario())
