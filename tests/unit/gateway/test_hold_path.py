"""The hold in the request path (`rhi_gateway.app`, `rhi_gateway.proxy`; system design, sections 5.3 and 6.2).

The gateway runs whole, over the in-memory key store and ledger, with the real `Readiness`
reading a target group that turns healthy at a set time. The test refreshes it every 5 seconds of
a clock it advances one second at a time: the hold, the readiness reads, the engine and the
proxy's time limit all wait on that clock. The engine is an `httpx.MockTransport` handler that
stays silent for as long as it is told. The client is driven over raw ASGI, and each piece of the
answer is recorded with the second it arrived.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.types import Message

from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.records import MODE_RECORD, Mode, PoolState
from rhi_common.students import StudentKey, utc_day
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger
from rhi_gateway.errors import WireFormat, render_json, render_stream_event
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.hold import keep_alive
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.readiness import Readiness

NOW = 1_800_000_000
STUDENT = 4242
ENGINE_TEXT = "engine-internal-detail-7f3a"
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
POOLS = parse_pools({"coursework": MOCK})
POOL = POOLS[0]


def sse(*events: tuple[str | None, dict[str, Any] | str]) -> bytes:
    lines = b""
    for name, data in events:
        text = data if isinstance(data, str) else json.dumps(data)
        lines += (f"event: {name}\n" if name else "").encode() + f"data: {text}\n\n".encode()
    return lines


# A whole stream of each format, ending cleanly, and a request body that asks for it.
STREAMS = {
    routes.MESSAGES: sse(
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 21, "output_tokens": 1}}}),
        ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}),
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
        ),
        ("message_stop", {"type": "message_stop"}),
    ),
    routes.CHAT_COMPLETIONS: sse(
        (None, {"choices": [{"index": 0, "delta": {"content": "Hi"}}], "usage": None}),
        (None, {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 9}}),
        (None, "[DONE]"),
    ),
    routes.RESPONSES: sse(
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Hi"}),
        (
            "response.completed",
            {"type": "response.completed", "response": {"usage": {"input_tokens": 40, "output_tokens": 11}}},
        ),
    ),
}
REPORTED = {routes.MESSAGES: (21, 7), routes.CHAT_COMPLETIONS: (30, 9), routes.RESPONSES: (40, 11)}
BODIES: dict[routes.Route, dict[str, Any]] = {
    routes.MESSAGES: {"model": "coursework", "max_tokens": 100, "messages": []},
    routes.CHAT_COMPLETIONS: {"model": "coursework", "stream_options": {"include_usage": True}, "messages": []},
    routes.RESPONSES: {"model": "coursework", "input": "hello"},
    routes.COUNT_TOKENS: {"model": "coursework", "messages": []},
}
FORMATS = list(STREAMS)


def wire_format(entry: routes.Route) -> WireFormat:
    assert entry.wire_format is not None
    return entry.wire_format


async def idle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


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

    async def wake_due(self) -> None:
        """Wake every sleeper whose time has come, and those that this wakes in turn."""
        while due := [wake for at, wake in self._sleepers if at <= self.now and not wake.done()]:
            for wake in due:
                wake.set_result(None)
            await idle()
        self._sleepers = [(at, wake) for at, wake in self._sleepers if not wake.done()]


class TargetGroup:
    """`describe_target_health`, in the test: one target, healthy from `healthy_at`; every read fails while `failing`."""

    def __init__(self, clock: Clock, healthy_at: float | None) -> None:
        self._clock = clock
        self.healthy_at = healthy_at
        self.failing = False

    def describe_target_health(self, TargetGroupArn: str) -> dict[str, Any]:
        if self.failing:
            raise ConnectionError("the read failed")
        healthy = self.healthy_at is not None and self._clock.now >= self.healthy_at
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy" if healthy else "initial"}}]}


class Records:
    """The pool-state table: the admin's mode record when one is given, else none (the configured mode); no note."""

    def __init__(self, mode: Mode | None = None) -> None:
        self.mode = mode

    def get_item(self, Key: dict[str, str]) -> dict[str, Any]:
        if self.mode is None or Key["record"] != MODE_RECORD:
            return {}
        return {"Item": PoolState("coursework", self.mode, set_by="admin", set_at=NOW).to_item()}


class Engine:
    """The pool's engine: silent for `headers_after`, then `first_after` more seconds, then `answer`, or it breaks off.

    With `rest_after`, only the answer's first byte comes then, and the rest that many seconds later.
    """

    def __init__(
        self,
        clock: Clock,
        answer: bytes,
        *,
        status: int = 200,
        headers_after: float = 0,
        first_after: float = 0,
        content_type: str = "text/event-stream",
        fails: bool = False,
        rest_after: float | None = None,
    ) -> None:
        self._clock = clock
        self.fails = fails
        self.answer = answer
        self.status = status
        self.headers_after = headers_after
        self.first_after = first_after
        self.content_type = content_type
        self.rest_after = rest_after
        self.reached_at: list[float] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.reached_at.append(self._clock.now - NOW)
        await self._clock.sleep(self.headers_after)

        async def body() -> AsyncIterator[bytes]:
            await self._clock.sleep(self.first_after)
            if self.fails:
                raise httpx.ReadError(ENGINE_TEXT)
            if self.rest_after is None:
                yield self.answer
                return
            yield self.answer[:1]
            await self._clock.sleep(self.rest_after)
            yield self.answer[1:]

        headers = {"content-type": self.content_type, "x-engine-detail": ENGINE_TEXT}
        return httpx.Response(self.status, headers=headers, content=body())


class Ledger(InMemoryLedger):
    """The in-memory ledger, recording each settlement."""

    def __init__(self) -> None:
        super().__init__(clock=lambda: float(NOW))
        self.reservations = 0
        self.settled: list[tuple[int, int, bool]] = []

    def reserve(self, *arguments: Any, **named: Any) -> Any:
        self.reservations += 1
        return super().reserve(*arguments, **named)

    def settle(self, reservation: Any, *, input_tokens: int, output_tokens: int, ended_cleanly: bool) -> int | None:
        self.settled.append((input_tokens, output_tokens, ended_cleanly))
        return super().settle(
            reservation, input_tokens=input_tokens, output_tokens=output_tokens, ended_cleanly=ended_cleanly
        )

    def open_leases(self) -> dict[str, Any]:
        day = self.day(STUDENT, utc_day(NOW))
        return {} if day is None else dict(day.leases)


class Gateway:
    """The whole gateway, its pool's target group turning healthy at `healthy_at` seconds (never, if None)."""

    def __init__(
        self, engine_answer: bytes = b"", healthy_at: float | None = 30, mode: Mode | None = None, **engine: Any
    ) -> None:
        self.clock = Clock()
        self.target_group = TargetGroup(self.clock, None if healthy_at is None else NOW + healthy_at)
        self.readiness = Readiness(
            {"coursework": "coursework-targets"}, elbv2=self.target_group, table=Records(mode), clock=self.clock.time
        )
        self.readiness.refresh()
        self.engine = Engine(self.clock, engine_answer, **engine)
        self.wakes: list[float] = []
        self.ledger = Ledger()
        self.key = generate_key()
        student = StudentKey(
            key_hash=key_hash(self.key),
            github_id=STUDENT,
            alias="otter",
            installation=None,
            issued_at=NOW - 3600,
            expires_at=NOW + 90 * 86400,
            revoked_at=None,
            daily_token_budget=2_000_000,
            concurrency_limit=4,
        )
        filters = {
            routes.MESSAGES: BodyFilter(messages.MESSAGES, POOLS),
            routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, POOLS),
            routes.CHAT_COMPLETIONS: ChatBodyFilter(POOLS),
            routes.RESPONSES: BodyFilter(responses.RESPONSES, POOLS),
        }
        client = proxy.upstream_client(httpx.MockTransport(self.engine))
        forwarding = Forwarding(
            authenticator=Authenticator(InMemoryKeyStore(student), wall_clock=lambda: float(NOW)),
            ledger=self.ledger,
            filters=filters,
            proxy=proxy.Proxy(
                client, "http://pools.example.com:8080", {"coursework": "secret"}, sleep=self.clock.sleep
            ),
            holding=Holding(
                views=self.readiness.views,
                wake=lambda pool: self.wakes.append(self.clock.now - NOW),
                clock=self.clock.time,
                sleep=self.clock.sleep,
            ),
        )
        self.app = build_app(forwarding_handlers(forwarding))

    async def advance_to(self, second: int) -> None:
        """Move the clock one second at a time, the pool's health read every 5 seconds, as the gateway reads it."""
        while self.clock.now < NOW + second:
            self.clock.now += 1
            if (self.clock.now - NOW) % 5 == 0:
                self.readiness.refresh()
            await self.clock.wake_due()


class Call:
    """One client's request over ASGI: its status, headers, and each piece of its body with the second it arrived."""

    def __init__(self, gateway: Gateway, entry: routes.Route, *, stream: bool = True) -> None:
        body = dict(BODIES[entry])
        if entry != routes.COUNT_TOKENS:
            body["stream"] = stream
        self._request: list[Message] = [{"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}]
        self._clock = gateway.clock
        self._left = asyncio.Event()
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.pieces: list[tuple[float, bytes]] = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": entry.path,
            "raw_path": entry.path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"x-api-key", gateway.key.encode()), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("gateway.example.com", 443),
        }
        self.responses_started = 0
        self.task = asyncio.ensure_future(gateway.app(scope, self._receive, self._send))

    async def _receive(self) -> Message:
        if self._request:
            return self._request.pop()
        await self._left.wait()  # The client stays until the test makes it leave.
        return {"type": "http.disconnect"}

    def leave(self) -> None:
        self._left.set()

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.responses_started += 1
            self.status = message["status"]
            self.headers = {name.decode().lower(): value.decode() for name, value in message.get("headers", [])}
        elif message["type"] == "http.response.body" and message.get("body"):
            self.pieces.append((self._clock.now - NOW, message["body"]))

    @property
    def body(self) -> bytes:
        return b"".join(piece for _, piece in self.pieces)

    def split(self, entry: routes.Route) -> tuple[list[float], bytes]:
        """The seconds of the keep-alives that came first, and every byte after them."""
        beat = keep_alive(wire_format(entry)).data
        times: list[float] = []
        for at, piece in self.pieces:
            if piece != beat:
                break
            times.append(at)
        return times, self.body[len(beat) * len(times) :]

    async def ended(self) -> None:
        # A guard, not the clock under test: every end here comes from the test's own steps.
        await asyncio.wait_for(self.task, timeout=10)


async def held(gateway: Gateway) -> None:
    """Wait until the request is held: its first wake was asked, once its reservation was made in a worker thread."""

    async def asked() -> None:
        while not gateway.wakes:
            await asyncio.sleep(0.001)

    # A guard, not the clock under test, which does not move meanwhile.
    await asyncio.wait_for(asked(), timeout=10)


def run(scenario: Any) -> None:
    asyncio.run(scenario())


@pytest.mark.parametrize("entry", FORMATS, ids=lambda entry: entry.path)
def test_a_held_stream_is_kept_alive_until_the_engine_speaks(entry: routes.Route) -> None:
    async def scenario() -> None:
        # Healthy at 30 seconds; the engine then sends its headers after 20 and its first event after 40.
        gateway = Gateway(STREAMS[entry], healthy_at=30, headers_after=20, first_after=20)
        call = Call(gateway, entry)
        await held(gateway)
        assert call.status == 200  # At once, before the pool is ready.
        assert gateway.wakes == [0]

        await gateway.advance_to(29)
        assert gateway.engine.reached_at == []
        await gateway.advance_to(80)
        await call.ended()

        times, rest = call.split(entry)
        assert gateway.engine.reached_at == [30]
        assert gateway.wakes == [0, 15]  # Asked again every 15 seconds, and never once ready.
        # A keep-alive every 10 seconds through the 70, none after the engine's first event, which came at 70.
        # (The one due at 70 itself may come just before that event.)
        assert times[:6] == [10, 20, 30, 40, 50, 60]
        assert times[6:] in ([], [70])
        assert rest == STREAMS[entry]
        assert [at for at, piece in call.pieces if piece != keep_alive(wire_format(entry)).data] == [70]
        assert call.responses_started == 1  # One answer: the real stream followed on the same connection.
        assert gateway.ledger.settled == [(*REPORTED[entry], True)]
        assert gateway.ledger.open_leases() == {}

    run(scenario)


@pytest.mark.parametrize("entry", FORMATS, ids=lambda entry: entry.path)
def test_keep_alives_last_until_the_first_event_is_whole(entry: routes.Route) -> None:
    async def scenario() -> None:
        # Forwarded at 30; the engine's first byte comes at once, the rest of its first event at 70.
        gateway = Gateway(STREAMS[entry], healthy_at=30, rest_after=40)
        call = Call(gateway, entry)
        await held(gateway)
        await gateway.advance_to(80)
        await call.ended()
        times, rest = call.split(entry)
        assert times[:6] == [10, 20, 30, 40, 50, 60]
        assert times[6:] in ([], [70])
        assert rest == STREAMS[entry]  # No keep-alive inside the engine's event.
        assert gateway.ledger.settled == [(*REPORTED[entry], True)]

    run(scenario)


@pytest.mark.parametrize("entry", FORMATS, ids=lambda entry: entry.path)
def test_the_deadline_ends_cleanly(entry: routes.Route) -> None:
    async def scenario() -> None:
        # A stream, held its 420 seconds on a pool that never turns healthy.
        gateway = Gateway(healthy_at=None)
        call = Call(gateway, entry)
        await held(gateway)
        await gateway.advance_to(POOL.hold_seconds - 1)
        assert not call.task.done()
        await gateway.advance_to(POOL.hold_seconds)
        await call.ended()
        times, rest = call.split(entry)
        assert call.status == 200
        assert len(times) == POOL.hold_seconds // 10 - 1
        assert rest == render_stream_event(Cause.WARMING, wire_format(entry))
        assert gateway.engine.reached_at == []
        assert gateway.ledger.settled == [(0, 0, False)]
        assert gateway.ledger.open_leases() == {}

        # A call that is not a stream, held its 240 seconds in silence.
        gateway = Gateway(healthy_at=None)
        call = Call(gateway, entry, stream=False)
        await held(gateway)
        await gateway.advance_to(POOL.hold_seconds_non_streaming - 1)
        assert not call.task.done()
        assert call.status is None
        await gateway.advance_to(POOL.hold_seconds_non_streaming)
        await call.ended()
        warming = render_json(Cause.WARMING, wire_format(entry))
        assert (call.status, call.body) == (503, warming.body)
        assert call.headers["retry-after"] == str(Cause.WARMING.retry_after)
        assert gateway.engine.reached_at == []
        assert gateway.ledger.settled == [(0, 0, False)]
        assert gateway.ledger.open_leases() == {}

    run(scenario)


def test_a_held_call_that_is_not_a_stream_is_forwarded_once_ready() -> None:
    async def scenario() -> None:
        answer = json.dumps({"type": "message", "usage": {"input_tokens": 21, "output_tokens": 7}}).encode()
        gateway = Gateway(answer, healthy_at=30, content_type="application/json")
        call = Call(gateway, routes.MESSAGES, stream=False)
        await held(gateway)
        await gateway.advance_to(30)
        await call.ended()
        assert (call.status, call.body) == (200, answer)
        assert call.pieces[0][0] == 30
        assert gateway.ledger.settled == [(21, 7, True)]

    run(scenario)


@pytest.mark.parametrize("entry", FORMATS, ids=lambda entry: entry.path)
def test_an_engine_refusal_after_the_hold(entry: routes.Route) -> None:
    async def scenario() -> None:
        refusal = json.dumps({"error": {"message": ENGINE_TEXT}}).encode()
        gateway = Gateway(refusal, healthy_at=30, status=400, content_type="application/json")
        call = Call(gateway, entry)
        await held(gateway)
        await gateway.advance_to(40)
        await call.ended()
        times, rest = call.split(entry)
        assert call.status == 200
        assert times == [10, 20, 30]
        assert gateway.engine.reached_at == [30]
        assert rest == render_stream_event(Cause.ENGINE_ERROR, wire_format(entry))
        assert ENGINE_TEXT.encode() not in call.body + json.dumps(call.headers).encode()
        assert gateway.ledger.settled == [(0, 0, False)]
        assert gateway.ledger.open_leases() == {}

    run(scenario)


@pytest.mark.parametrize("entry", FORMATS, ids=lambda entry: entry.path)
def test_an_engine_that_fails_before_its_first_event_ends_the_held_stream(entry: routes.Route) -> None:
    async def scenario() -> None:
        gateway = Gateway(STREAMS[entry], healthy_at=30, first_after=20, fails=True)
        call = Call(gateway, entry)
        await held(gateway)
        await gateway.advance_to(60)
        await call.ended()
        times, rest = call.split(entry)
        assert times == [10, 20, 30, 40, 50]
        assert rest == render_stream_event(Cause.ENGINE_ERROR, wire_format(entry))
        assert ENGINE_TEXT.encode() not in call.body
        assert gateway.ledger.settled == [(0, 0, False)]

    run(scenario)


def test_a_failed_read_keeps_the_hold() -> None:
    async def scenario() -> None:
        # The target is healthy from 30 seconds, but every read fails until 60.
        gateway = Gateway(STREAMS[routes.MESSAGES], healthy_at=30)
        gateway.target_group.failing = True
        call = Call(gateway, routes.MESSAGES)
        await held(gateway)
        await gateway.advance_to(60)
        times, rest = call.split(routes.MESSAGES)
        assert gateway.engine.reached_at == []  # Not forwarded,
        assert not call.task.done()  # and not ended: the stream is open,
        assert times == [10, 20, 30, 40, 50, 60]  # still kept alive,
        assert rest == b""  # with no error event.
        assert gateway.ledger.settled == []

        gateway.target_group.failing = False
        await gateway.advance_to(70)
        await call.ended()
        assert gateway.engine.reached_at == [65]  # The first read that succeeds.
        assert call.split(routes.MESSAGES)[1] == STREAMS[routes.MESSAGES]
        assert gateway.ledger.settled == [(*REPORTED[routes.MESSAGES], True)]

    run(scenario)


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "not a stream"])
def test_a_client_that_leaves_during_the_hold_is_settled_once(stream: bool) -> None:
    async def scenario() -> None:
        gateway = Gateway(STREAMS[routes.MESSAGES], healthy_at=None)
        call = Call(gateway, routes.MESSAGES, stream=stream)
        await held(gateway)
        await gateway.advance_to(20)
        call.leave()
        await call.ended()
        await gateway.advance_to(60)
        assert gateway.wakes == [0, 15]  # No wake once the client has gone.
        assert gateway.engine.reached_at == []
        assert gateway.ledger.settled == [(0, 0, False)]
        assert gateway.ledger.open_leases() == {}

    run(scenario)


def test_token_counting_never_waits() -> None:
    async def scenario() -> None:
        gateway = Gateway(b'{"input_tokens":17}', healthy_at=None, content_type="application/json")
        call = Call(gateway, routes.COUNT_TOKENS)
        await call.ended()  # The clock never moves.
        warming = render_json(Cause.WARMING, WireFormat.MESSAGES)
        assert (call.status, call.body) == (503, warming.body)
        assert call.headers["retry-after"] == str(Cause.WARMING.retry_after)
        assert gateway.wakes == []
        assert gateway.engine.reached_at == []
        assert gateway.ledger.reservations == 0
        assert gateway.ledger.open_leases() == {}

    run(scenario)


def test_a_disabled_pool_is_refused_at_once_with_no_reservation() -> None:
    async def scenario() -> None:
        gateway = Gateway(healthy_at=0, mode=Mode.DISABLED)
        call = Call(gateway, routes.MESSAGES)
        await call.ended()
        assert (call.status, call.body) == (503, render_json(Cause.POOL_DISABLED, WireFormat.MESSAGES).body)
        assert gateway.wakes == []
        assert gateway.engine.reached_at == []
        assert gateway.ledger.reservations == 0

    run(scenario)
