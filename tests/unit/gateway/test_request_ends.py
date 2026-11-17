"""Every end of a forwarded request: settled once, and told to the client by its cause (`rhi_gateway.proxy`).

The gateway runs whole, over the in-memory key store and a ledger that records each settlement.
The upstream is an `httpx.MockTransport` handler written here, which can end well, fail, stop early
or stall. Time is a clock the test advances: the proxy waits for its limit with the clock's `sleep`.
The client is driven over raw ASGI, so that it can leave in the middle of a stream.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.types import Message

from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.students import StudentKey, utc_day
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger, Reservation
from rhi_gateway.errors import WireFormat, render_json, render_stream_event
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore

NOW = 1_800_000_000
STUDENT = 4242
LIMIT = 1800
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


def sse(*events: tuple[str | None, dict[str, Any]]) -> bytes:
    return b"".join(
        (f"event: {name}\n" if name else "").encode() + f"data: {json.dumps(data)}\n\n".encode()
        for name, data in events
    )


# The start of an answer in each format, cut at the end of an event; then a clean end of a Messages one.
MESSAGES_START = sse(
    ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 5000, "output_tokens": 1}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
)
MESSAGES_END = sse(
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
    ("message_stop", {"type": "message_stop"}),
)
STARTS = {
    routes.MESSAGES: MESSAGES_START,
    routes.CHAT_COMPLETIONS: sse((None, {"choices": [{"index": 0, "delta": {"content": "Hi"}}], "usage": None})),
    routes.RESPONSES: sse(("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Hi"})),
}
BODIES: dict[routes.Route, dict[str, Any]] = {
    routes.MESSAGES: {"model": "coursework", "max_tokens": 100, "messages": []},
    routes.CHAT_COMPLETIONS: {"model": "coursework", "stream_options": {"include_usage": True}, "messages": []},
    routes.RESPONSES: {"model": "coursework", "input": "hello"},
}
FORMATS = list(STARTS)


async def _forever() -> None:
    await asyncio.get_running_loop().create_future()


class Upstream:
    """The engine, in the test: it answers `parts`, then ends, fails, stalls; or never answers at all."""

    def __init__(self, parts: list[bytes], then: str = "end", *, status: int = 200, content_type: str = "") -> None:
        self.parts = parts
        self.then = then
        self.status = status
        self.content_type = content_type or "text/event-stream"
        self.reached = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.reached.set()
        if self.then == "never":
            await _forever()

        async def body() -> AsyncIterator[bytes]:
            for part in self.parts:
                yield part
            if self.then == "fail":
                raise httpx.ReadError(ENGINE_TEXT)
            if self.then == "stall":
                await _forever()

        headers = {"content-type": self.content_type, "x-engine-detail": ENGINE_TEXT}
        return httpx.Response(self.status, headers=headers, content=body())


class Clock:
    """The time the proxy's limit is counted on: it moves only when the test advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, seconds: float) -> None:
        wake = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, wake))
        await wake

    async def advance_to(self, moment: float) -> None:
        self.now = moment
        for at, wake in self._sleepers:
            if at <= moment and not wake.done():
                wake.set_result(None)
        await idle()


async def idle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


class Ledger(InMemoryLedger):
    """The in-memory ledger, recording each reservation and each settlement with what it charged."""

    def __init__(self) -> None:
        super().__init__(clock=lambda: float(NOW))
        self.reserved: list[Reservation] = []
        self.settled: list[tuple[int, int, bool, int | None]] = []

    def reserve(self, *arguments: Any, **named: Any) -> Reservation:
        reservation = super().reserve(*arguments, **named)
        self.reserved.append(reservation)
        return reservation

    def settle(
        self, reservation: Reservation, *, input_tokens: int, output_tokens: int, ended_cleanly: bool
    ) -> int | None:
        charge = super().settle(
            reservation, input_tokens=input_tokens, output_tokens=output_tokens, ended_cleanly=ended_cleanly
        )
        self.settled.append((input_tokens, output_tokens, ended_cleanly, charge))
        return charge

    @property
    def estimate(self) -> int:
        return self.reserved[0].lease.estimate

    def leases(self) -> dict[str, Any]:
        day = self.day(STUDENT, utc_day(NOW))
        assert day is not None
        return dict(day.leases)


def gateway(upstream: Upstream, clock: Clock) -> tuple[Starlette, str, Ledger]:
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
    ledger = Ledger()
    filters = {
        routes.MESSAGES: BodyFilter(messages.MESSAGES, POOLS),
        routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, POOLS),
        routes.CHAT_COMPLETIONS: ChatBodyFilter(POOLS),
        routes.RESPONSES: BodyFilter(responses.RESPONSES, POOLS),
    }
    client = proxy.upstream_client(httpx.MockTransport(upstream))
    forwarding = Forwarding(
        authenticator=Authenticator(InMemoryKeyStore(item), wall_clock=lambda: float(NOW)),
        ledger=ledger,
        filters=filters,
        proxy=proxy.Proxy(client, "http://pools.example.com:8080", {"coursework": "secret"}, sleep=clock.sleep),
    )
    return build_app(forwarding_handlers(forwarding)), key, ledger


class Call:
    """One client's request over ASGI: what it was answered, and a way to leave mid-answer."""

    def __init__(self, app: Starlette, key: str, entry: routes.Route, *, stream: bool = True) -> None:
        body = json.dumps({**BODIES[entry], "stream": stream}).encode()
        self._request: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]
        self._left = asyncio.Event()
        self.status: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = b""
        self.got_some = asyncio.Event()
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
            "headers": [(b"x-api-key", key.encode()), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("gateway.example.com", 443),
        }
        self.task = asyncio.ensure_future(app(scope, self._receive, self._send))

    async def _receive(self) -> Message:
        if self._request:
            return self._request.pop()
        await self._left.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body" and message.get("body"):
            self.body += message["body"]
            self.got_some.set()

    def leave(self) -> None:
        self._left.set()

    async def ended(self) -> None:
        # A guard, not the clock under test: every end here comes from the test's own steps.
        await asyncio.wait_for(self.task, timeout=10)


def run(scenario: Any) -> None:
    asyncio.run(scenario())


WHOLE_ANSWER = json.dumps({"type": "message", "usage": {"input_tokens": 5000, "output_tokens": 7}}).encode()


@pytest.mark.parametrize("end", ["clean", "clean, not a stream", "client leaves", "upstream fails", "time limit"])
def test_every_end_settles_once(end: str) -> None:
    async def scenario() -> None:
        clock = Clock()
        if end == "clean, not a stream":
            upstream = Upstream([WHOLE_ANSWER], content_type="application/json")
        else:
            then = {"clean": "end", "client leaves": "stall", "upstream fails": "fail", "time limit": "stall"}[end]
            upstream = Upstream([MESSAGES_START, MESSAGES_END] if end == "clean" else [MESSAGES_START], then)
        app, key, ledger = gateway(upstream, clock)
        call = Call(app, key, routes.MESSAGES, stream=end != "clean, not a stream")
        await call.got_some.wait()
        if end == "client leaves":
            call.leave()
        if end == "time limit":
            await clock.advance_to(LIMIT)
        await call.ended()
        # Nothing later settles it again: not the time limit, nor anything left running.
        await clock.advance_to(2 * LIMIT)

        assert len(ledger.settled) == 1
        assert ledger.leases() == {}
        input_tokens, output_tokens, clean, charge = ledger.settled[0]
        if end.startswith("clean"):
            assert (input_tokens, output_tokens, clean, charge) == (5000, 7, True, 5007)
        else:
            # The tokens observed (the start reported 5,000 in and 1 out), never less than the estimate.
            assert (input_tokens, output_tokens, clean) == (5000, 1, False)
            assert charge == max(5001, ledger.estimate)
            assert charge is not None and charge >= ledger.estimate

    run(scenario)


@pytest.mark.parametrize("entry", FORMATS, ids=lambda entry: entry.path)
def test_engine_error(entry: routes.Route) -> None:
    wire_format = entry.wire_format
    assert isinstance(wire_format, WireFormat)

    async def scenario() -> None:
        # Before anything was sent: an engine that answers 500, with its own text in body and headers.
        failed = Upstream([json.dumps({"error": ENGINE_TEXT}).encode()], status=500, content_type="application/json")
        app, key, ledger = gateway(failed, Clock())
        call = Call(app, key, entry)
        await call.ended()
        expected = render_json(Cause.ENGINE_ERROR, wire_format)
        assert (call.status, call.body) == (502, expected.body)
        assert ENGINE_TEXT.encode() not in call.body + b"".join(name + value for name, value in call.headers)
        assert len(ledger.settled) == 1

        # On a stream that has started: one that stops early, and one that breaks off with an error.
        for then in ("end", "fail"):
            app, key, ledger = gateway(Upstream([STARTS[entry]], then), Clock())
            call = Call(app, key, entry)
            await call.ended()
            assert call.status == 200
            assert call.body == STARTS[entry] + render_stream_event(Cause.ENGINE_ERROR, wire_format)
            assert ENGINE_TEXT.encode() not in call.body + b"".join(name + value for name, value in call.headers)
            assert len(ledger.settled) == 1

    run(scenario)


@pytest.mark.parametrize("entry", FORMATS, ids=lambda entry: entry.path)
def test_time_limit(entry: routes.Route) -> None:
    wire_format = entry.wire_format
    assert isinstance(wire_format, WireFormat)

    async def scenario() -> None:
        # A call that is not a stream, to an engine that never answers.
        clock = Clock()
        upstream = Upstream([], "never")
        app, key, ledger = gateway(upstream, clock)
        call = Call(app, key, entry, stream=False)
        await upstream.reached.wait()
        await clock.advance_to(LIMIT - 1)
        assert not call.task.done()
        await clock.advance_to(LIMIT)
        await call.ended()
        assert (call.status, call.body) == (504, render_json(Cause.TIME_LIMIT, wire_format).body)
        assert [charge for *_, charge in ledger.settled] == [ledger.estimate]

        # A stream that has started, then goes quiet.
        clock = Clock()
        app, key, ledger = gateway(Upstream([STARTS[entry]], "stall"), clock)
        call = Call(app, key, entry)
        await call.got_some.wait()
        await clock.advance_to(LIMIT - 1)
        assert not call.task.done()
        await clock.advance_to(LIMIT)
        await call.ended()
        assert call.status == 200
        assert call.body == STARTS[entry] + render_stream_event(Cause.TIME_LIMIT, wire_format)
        assert len(ledger.settled) == 1

    run(scenario)


@pytest.mark.parametrize("entry", FORMATS, ids=lambda entry: entry.path)
def test_an_error_event_of_the_engine_is_replaced(entry: routes.Route) -> None:
    wire_format = entry.wire_format
    assert isinstance(wire_format, WireFormat)
    error = {
        routes.MESSAGES: sse(("error", {"type": "error", "error": {"type": "overloaded", "message": ENGINE_TEXT}})),
        routes.CHAT_COMPLETIONS: sse((None, {"error": {"message": ENGINE_TEXT}})),
        routes.RESPONSES: sse(
            (
                "response.failed",
                {"type": "response.failed", "response": {"error": {"message": ENGINE_TEXT}, "usage": None}},
            )
        ),
    }[entry]

    async def scenario() -> None:
        # The engine's error event is cut across chunks, as a connection can cut it.
        parts = [STARTS[entry], *(error[at : at + 7] for at in range(0, len(error), 7))]
        app, key, ledger = gateway(Upstream(parts, "stall"), Clock())
        call = Call(app, key, entry)
        await call.ended()
        assert call.body == STARTS[entry] + render_stream_event(Cause.ENGINE_ERROR, wire_format)
        assert ENGINE_TEXT.encode() not in call.body
        assert [clean for _, _, clean, _ in ledger.settled] == [False]

    run(scenario)


@pytest.mark.parametrize(
    ("entry", "answer"),
    [
        (routes.MESSAGES, WHOLE_ANSWER[:-1]),
        (routes.RESPONSES, json.dumps({"status": "failed", "error": {"message": ENGINE_TEXT}}).encode()),
    ],
    ids=["cut short", "failed"],
)
def test_an_answer_that_is_not_whole_is_an_engine_error(entry: routes.Route, answer: bytes) -> None:
    wire_format = entry.wire_format
    assert isinstance(wire_format, WireFormat)

    async def scenario() -> None:
        app, key, ledger = gateway(Upstream([answer], content_type="application/json"), Clock())
        call = Call(app, key, entry, stream=False)
        await call.ended()
        assert (call.status, call.body) == (502, render_json(Cause.ENGINE_ERROR, wire_format).body)
        assert [(clean, charge) for _, _, clean, charge in ledger.settled] == [(False, ledger.estimate)]

    run(scenario)


def test_an_answer_that_breaks_off_is_charged_what_it_reported() -> None:
    async def scenario() -> None:
        app, key, ledger = gateway(Upstream([WHOLE_ANSWER], "fail", content_type="application/json"), Clock())
        call = Call(app, key, routes.MESSAGES, stream=False)
        await call.ended()
        assert call.status == 502
        assert ledger.settled == [(5000, 7, False, max(5007, ledger.estimate))]

    run(scenario)


def test_a_client_that_leaves_before_its_answer_is_settled_at_once() -> None:
    async def scenario() -> None:
        upstream = Upstream([], "never")
        app, key, ledger = gateway(upstream, Clock())
        call = Call(app, key, routes.MESSAGES, stream=False)
        await upstream.reached.wait()
        call.leave()
        await call.ended()  # The clock never moves: only the client's leaving can end it.
        assert [charge for *_, charge in ledger.settled] == [ledger.estimate]
        assert ledger.leases() == {}

    run(scenario)


def test_a_limit_already_reached_wins_over_a_waiting_chunk() -> None:
    async def scenario() -> None:
        clock = Clock()
        app, key, ledger = gateway(Upstream([MESSAGES_START, MESSAGES_END]), clock)

        class Slow(Call):
            async def _send(self, message: Message) -> None:
                await super()._send(message)
                if message.get("body") == MESSAGES_START:
                    # Sending the first chunk takes the request past its limit; the clean end is ready.
                    await clock.advance_to(LIMIT)

        call = Slow(app, key, routes.MESSAGES)
        await call.ended()
        assert call.body == MESSAGES_START + render_stream_event(Cause.TIME_LIMIT, WireFormat.MESSAGES)
        assert [clean for _, _, clean, _ in ledger.settled] == [False]

    run(scenario)


@pytest.mark.parametrize("entry", FORMATS, ids=lambda entry: entry.path)
def test_an_error_event_whose_data_spans_lines_is_replaced(entry: routes.Route) -> None:
    wire_format = entry.wire_format
    assert isinstance(wire_format, WireFormat)
    name, data = {
        routes.MESSAGES: ("error", {"type": "error", "error": {"message": ENGINE_TEXT}}),
        routes.CHAT_COMPLETIONS: (None, {"error": {"message": ENGINE_TEXT}}),
        routes.RESPONSES: (
            "response.failed",
            {"type": "response.failed", "response": {"error": {"message": ENGINE_TEXT}}},
        ),
    }[entry]
    # One data value carried by several `data:` lines, which the reader joins.
    lines = "".join(f"data: {line}\n" for line in json.dumps(data, indent=2).splitlines())
    error = ((f"event: {name}\n" if name else "") + lines + "\n").encode()

    async def scenario() -> None:
        app, key, ledger = gateway(Upstream([STARTS[entry], error]), Clock())
        call = Call(app, key, entry)
        await call.ended()
        assert call.body == STARTS[entry] + render_stream_event(Cause.ENGINE_ERROR, wire_format)
        assert len(ledger.settled) == 1

    run(scenario)


def test_an_answer_of_an_unexpected_shape_is_settled() -> None:
    async def scenario() -> None:
        answer = json.dumps({"type": [], "usage": {"input_tokens": 5000, "output_tokens": 7}}).encode()
        app, key, ledger = gateway(Upstream([answer], content_type="application/json"), Clock())
        call = Call(app, key, routes.MESSAGES, stream=False)
        await call.ended()
        assert len(ledger.settled) == 1
        assert ledger.leases() == {}

    run(scenario)


def test_a_limit_reached_with_the_end_of_an_answer_wins() -> None:
    async def scenario() -> None:
        clock = Clock()

        async def upstream(request: httpx.Request) -> httpx.Response:
            async def body() -> AsyncIterator[bytes]:
                yield WHOLE_ANSWER
                # The limit is reached as the answer ends: both are ready together.
                clock.now = LIMIT
                for at, wake in clock._sleepers:
                    if at <= LIMIT and not wake.done():
                        wake.set_result(None)

            return httpx.Response(200, headers={"content-type": "application/json"}, content=body())

        app, key, ledger = gateway(upstream, clock)  # ty: ignore[invalid-argument-type] - a bare handler stands in for the Upstream
        call = Call(app, key, routes.MESSAGES, stream=False)
        await call.ended()
        assert (call.status, call.body) == (504, render_json(Cause.TIME_LIMIT, WireFormat.MESSAGES).body)
        assert [clean for _, _, clean, _ in ledger.settled] == [False]

    run(scenario)


async def settled(ledger: Ledger) -> None:
    while not ledger.settled:
        await asyncio.sleep(0.001)


def test_the_limit_ends_a_request_whose_client_does_not_take_a_write() -> None:
    async def scenario() -> None:
        clock = Clock()
        upstream = Upstream([MESSAGES_START], "stall")
        app, key, ledger = gateway(upstream, clock)
        taken = asyncio.Event()

        class Stuck(Call):
            async def _send(self, message: Message) -> None:
                await super()._send(message)
                if message.get("body") == MESSAGES_START:
                    await taken.wait()  # The client does not take this write until the test lets it.

        call = Stuck(app, key, routes.MESSAGES)
        await call.got_some.wait()
        await clock.advance_to(LIMIT)
        # Settled at the limit, the write never taken: the client is not waited for. The ledger is
        # called in a worker thread, hence the wait, with a guard that is not the clock under test.
        await asyncio.wait_for(settled(ledger), timeout=10)
        assert [clean for _, _, clean, _ in ledger.settled] == [False]
        assert ledger.leases() == {}
        assert not taken.is_set()
        await call.ended()
        assert call.body == MESSAGES_START + render_stream_event(Cause.TIME_LIMIT, WireFormat.MESSAGES)
        assert len(ledger.settled) == 1

    run(scenario)


@pytest.mark.parametrize("blocked", ["start", "clean end"])
def test_the_limit_is_told_after_a_write_given_up(blocked: str) -> None:
    async def scenario() -> None:
        clock = Clock()
        app, key, ledger = gateway(Upstream([MESSAGES_START, MESSAGES_END]), clock)
        writing = asyncio.Event()

        class Blocked(Call):
            async def _send(self, message: Message) -> None:
                at = (
                    message["type"] == "http.response.start"
                    if blocked == "start"
                    else message.get("body") == MESSAGES_END
                )
                if at and not writing.is_set():
                    writing.set()
                    await _forever()  # This write is never taken; the client takes the ones after it.
                await super()._send(message)

        call = Blocked(app, key, routes.MESSAGES)
        await asyncio.wait_for(writing.wait(), timeout=10)
        await clock.advance_to(LIMIT)
        await call.ended()
        if blocked == "start":
            assert (call.status, call.body) == (504, render_json(Cause.TIME_LIMIT, WireFormat.MESSAGES).body)
        else:
            # The clean end never reached the client: the answer did not end cleanly.
            assert call.body == MESSAGES_START + render_stream_event(Cause.TIME_LIMIT, WireFormat.MESSAGES)
        assert len(ledger.settled) == 1
        assert ledger.settled[0][2] is False
        assert ledger.settled[0][3] is not None and ledger.settled[0][3] >= ledger.estimate

    run(scenario)
