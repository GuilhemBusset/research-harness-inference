"""The gateway's log: one record of metadata per request, and its own faults told apart (`rhi_gateway.telemetry`).

The gateway runs whole over the in-memory key store and ledger; the engine is an
`httpx.MockTransport` that answers every forwarded route whole, or, for the waits a record notes,
streams the parts the test gives on the test's clock. The gateway is built without metrics: the
record notes its waits all the same. Records are read from standard output, where the gateway
writes them. Every key is generated when the tests run.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.students import StudentKey
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.hold import HELD_HEADER
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.readiness import PoolView, Readiness
from rhi_gateway.waker import Waker

NOW = 1_800_000_000
# The course end the ledger's rows expire by.
COURSE_END_DATE = date(2027, 6, 30)
GITHUB_ID = 98_765_431
MARKER = "marker-5c1e-not-for-the-log"
FIELDS = {
    "alias",
    "time",
    "pool",
    "route",
    "wire_format",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "time_to_first_token_ms",
    "held_ms",
    "latency_ms",
    "status",
    "cause",
}
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
        },
        "evening": {
            "engine": "mock",
            "instance_types": ["t3.small"],
            "prices": {"t3.small": 0.0208},
            "max_instances": 1,
            "warm_instances": 0,
            "wake": "scheduled-only",
            "target_concurrency": 8,
            "context_length": 131072,
            "largest_output": 8000,
        },
    }
)
# One answer every forwarded route reads as whole: 5,000 tokens in and 7 out (12 for a token count).
ENGINE_ANSWER = json.dumps(
    {
        "type": "message",
        "input_tokens": 12,
        "usage": {"input_tokens": 5000, "output_tokens": 7, "prompt_tokens": 5000, "completion_tokens": 7},
    }
).encode()
BODIES: dict[routes.Route, dict[str, Any]] = {
    routes.MESSAGES: {"model": "coursework", "max_tokens": 100, "messages": [{"role": "user", "content": MARKER}]},
    routes.COUNT_TOKENS: {"model": "coursework", "messages": [{"role": "user", "content": MARKER}]},
    routes.CHAT_COMPLETIONS: {"model": "coursework", "messages": [{"role": "user", "content": MARKER}]},
    routes.RESPONSES: {"model": "coursework", "input": MARKER},
    routes.WAKE: {"model": "coursework", "note": MARKER},
}


class Aws:
    """The AWS clients the readiness and the waker are given; none is reached by these tests' assertions."""

    def describe_target_health(self, **_: Any) -> dict[str, Any]:
        return {"TargetHealthDescriptions": []}

    def get_item(self, **_: Any) -> dict[str, Any]:
        return {}

    def invoke(self, **_: Any) -> dict[str, Any]:
        return {"StatusCode": 202}


def gateway(
    engine: httpx.MockTransport | None = None,
    holding: Holding | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Starlette, str]:
    """The whole gateway, every route served; and the key of its one student.

    By default the engine answers whole and the coursework pool is ready; a test may give its own
    engine, hold, and clock of a request's waits.
    """
    key = generate_key()
    item = StudentKey(
        key_hash=key_hash(key),
        github_id=GITHUB_ID,
        alias="otter",
        installation=None,
        issued_at=NOW - 3600,
        expires_at=NOW + 86400,
        revoked_at=None,
        daily_token_budget=2_000_000,
        concurrency_limit=4,
    )
    authenticator = Authenticator(InMemoryKeyStore(item), wall_clock=lambda: float(NOW))
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=lambda: float(NOW))
    # The coursework pool is ready; the evening pool, scheduled only, is not.
    ready = lambda: {"coursework": PoolView(ready=True)}
    if engine is None:
        engine = httpx.MockTransport(lambda request: httpx.Response(200, json=json.loads(ENGINE_ANSWER)))
    forwarding = Forwarding(
        authenticator=authenticator,
        ledger=ledger,
        filters={
            routes.MESSAGES: BodyFilter(messages.MESSAGES, POOLS),
            routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, POOLS),
            routes.CHAT_COMPLETIONS: ChatBodyFilter(POOLS),
            routes.RESPONSES: BodyFilter(responses.RESPONSES, POOLS),
        },
        proxy=proxy.Proxy(proxy.upstream_client(engine), "http://pools.example.com:8080", {"coursework": "secret"}),
        holding=Holding(views=ready, wake=lambda pool: None) if holding is None else holding,
        clock=clock,
    )
    readiness = Readiness({"coursework": "tg-coursework"}, elbv2=Aws(), table=Aws(), clock=lambda: float(NOW))
    local = LocalRoutes(
        authenticator,
        POOLS,
        ready,
        default_pool=POOLS[0],
        clock=lambda: float(NOW),
        waker=Waker(readiness, lambda_client=Aws(), function_name="scaler", clock=lambda: float(NOW)),
    )
    return build_app(forwarding_handlers(forwarding), local=local), key


def call(
    app: Starlette, method: str, path: str, *, key: str | None = None, body: Any = None, marker: bool = False
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """One request over ASGI: the status, headers and body answered."""
    headers = [(b"content-type", b"application/json")]
    if key is not None:
        headers.append((b"x-api-key", key.encode()))
    if marker:
        headers += [(b"x-marker", MARKER.encode()), (b"user-agent", MARKER.encode())]
    request: list[Message] = [
        {"type": "http.request", "body": b"" if body is None else json.dumps(body).encode(), "more_body": False}
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        if request:
            return request.pop()
        await asyncio.get_running_loop().create_future()  # The client never leaves.
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("192.0.2.1", 50000),
        "server": ("gateway.example.com", 443),
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    content = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return start["status"], list(start.get("headers", [])), content


def records(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """Every line written to standard output, each a JSON object."""
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_a_forwarded_request_gives_one_record_of_the_stated_fields(capsys: pytest.CaptureFixture[str]) -> None:
    app, key = gateway()
    status, _, _ = call(app, "POST", "/v1/messages", key=key, body=BODIES[routes.MESSAGES])
    assert status == 200
    [record] = records(capsys)
    assert set(record) == FIELDS
    assert datetime.fromisoformat(record.pop("time")).utcoffset() is not None
    assert isinstance(record.pop("latency_ms"), int)
    assert record == {
        "alias": "otter",
        "pool": "coursework",
        "route": "/v1/messages",
        "wire_format": "messages",
        "input_tokens": 5000,
        "output_tokens": 7,
        "cached_input_tokens": 0,
        "time_to_first_token_ms": None,  # Not a stream: no token was sent as one.
        "held_ms": 0,
        "status": 200,
        "cause": None,
    }


def test_a_refused_request_and_a_local_route_give_one_record_each(capsys: pytest.CaptureFixture[str]) -> None:
    app, key = gateway()
    assert call(app, "POST", "/v1/chat/completions", key=generate_key(), body=BODIES[routes.CHAT_COMPLETIONS])[0] == 401
    [refused] = records(capsys)
    assert set(refused) == FIELDS
    assert (refused["status"], refused["cause"], refused["alias"]) == (401, "invalid_key", None)
    assert refused["wire_format"] == "chat_completions"

    assert call(app, "GET", "/v1/models", key=key)[0] == 200
    [models] = records(capsys)
    assert set(models) == FIELDS
    assert (models["status"], models["cause"], models["alias"], models["wire_format"]) == (200, None, "otter", None)

    assert call(app, "HEAD", "/api/hello")[0] == 200
    [hello] = records(capsys)
    assert set(hello) == FIELDS
    assert (hello["status"], hello["cause"], hello["alias"]) == (200, None, None)

    assert call(app, "GET", "/v1/nowhere", key=key)[0] == 404
    [outside] = records(capsys)
    assert (outside["status"], outside["cause"]) == (404, "not_found")
    assert (refused["route"], models["route"], hello["route"], outside["route"]) == (
        "/v1/chat/completions",
        "/v1/models",
        "/api/hello",
        None,
    )
    # A path of the table with another method is outside the table too.
    assert call(app, "POST", "/v1/models", key=key)[0] == 404
    [other_method] = records(capsys)
    assert other_method["route"] is None


def test_nothing_of_a_request_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    app, key = gateway()
    for entry in routes.TABLE:
        call(app, entry.method, entry.path, key=key, body=BODIES.get(entry), marker=True)
    logged = capsys.readouterr().out
    assert len(logged.splitlines()) == len(routes.TABLE)
    # Neither the marker of the body and headers, nor the key, its hash or the student's GitHub ID.
    for secret in (MARKER, key, key_hash(key), str(GITHUB_ID)):
        assert secret not in logged


def sse(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


# A Messages stream: its usage (100 tokens not cached, 900 read from the cache), a token at 2.5
# seconds from the request's acceptance, and its end with 40 tokens out.
STREAM = [
    (
        0.0,
        sse(
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 100, "cache_read_input_tokens": 900}}},
        ),
    ),
    (2.5, sse("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}})),
    (
        3.0,
        sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 40}})
        + sse("message_stop", {"type": "message_stop"}),
    ),
]
# A stream cut before it reports any usage: the engine stops after a ping.
CUT = [(0.0, sse("ping", {"type": "ping"}))]


@dataclass
class Waits:
    """The test's clocks: the gateway's (0 when a request is accepted, moved by the engine's stream) and the hold's."""

    now: float = 0.0
    hold_clock: float = float(NOW)
    ready: bool = True  # Whether the pool is: a request for one that is not is held until the hold's first wait.
    sends: list[tuple[float, bytes]] = field(default_factory=list)


def streaming(waits: Waits) -> tuple[Starlette, str]:
    """The gateway, without metrics, over an engine that streams `waits.sends`, each part at its second."""

    async def engine(request: httpx.Request) -> httpx.Response:
        async def parts() -> AsyncIterator[bytes]:
            for at, part in waits.sends:
                waits.now = at
                yield part

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=parts())

    async def sleep(seconds: float) -> None:
        # The hold's first wait passes on the hold's clock, and the pool is ready at its end.
        if not waits.ready and seconds > 0:
            waits.hold_clock += seconds
            waits.ready = True
        await asyncio.sleep(0)

    holding = Holding(
        views=lambda: {"coursework": PoolView(ready=waits.ready)},
        wake=lambda pool: None,
        clock=lambda: waits.hold_clock,
        sleep=sleep,
    )
    return gateway(httpx.MockTransport(engine), holding, clock=lambda: waits.now)


def streamed(waits: Waits, *, ready: bool, sends: list[tuple[float, bytes]]) -> str:
    """One streamed Messages request, held when the pool is not `ready`, its body and headers carrying the marker; its key."""
    app, key = streaming(waits)
    waits.ready, waits.sends = ready, sends
    body = {**BODIES[routes.MESSAGES], "stream": True}
    status, headers, _ = call(app, "POST", "/v1/messages", key=key, body=body, marker=True)
    assert status == 200
    assert any(name == HELD_HEADER.encode() for name, _ in headers) is not ready
    return key


def test_a_held_stream_logs_its_route_tokens_and_waits(capsys: pytest.CaptureFixture[str]) -> None:
    waits = Waits()
    key = streamed(waits, ready=False, sends=STREAM)
    logged = capsys.readouterr().out
    [record] = [json.loads(line) for line in logged.splitlines()]
    assert set(record) == FIELDS
    assert record["route"] == "/v1/messages"
    # The tokens as the Messages parser gives them: the whole input, its cached part apart, and the output.
    assert (record["input_tokens"], record["cached_input_tokens"], record["output_tokens"]) == (1000, 900, 40)
    assert record["time_to_first_token_ms"] == 2500
    assert record["held_ms"] == round((waits.hold_clock - NOW) * 1000) > 0
    # Nothing of the request: not the marker of its body and headers, not its key, its hash or the GitHub ID.
    for secret in (MARKER, key, key_hash(key), str(GITHUB_ID)):
        assert secret not in logged


def test_a_warm_stream_was_not_held(capsys: pytest.CaptureFixture[str]) -> None:
    streamed(Waits(), ready=True, sends=STREAM)
    [record] = records(capsys)
    assert record["held_ms"] == 0
    assert record["time_to_first_token_ms"] == 2500


def test_a_stream_cut_before_any_usage_logs_no_tokens_and_no_first_token(capsys: pytest.CaptureFixture[str]) -> None:
    streamed(Waits(), ready=True, sends=CUT)
    [record] = records(capsys)
    assert record["route"] == "/v1/messages"
    assert (record["input_tokens"], record["cached_input_tokens"], record["output_tokens"]) == (0, 0, 0)
    assert record["time_to_first_token_ms"] is None


def test_a_token_count_logs_its_route_and_its_count(capsys: pytest.CaptureFixture[str]) -> None:
    app, key = gateway()
    assert call(app, "POST", "/v1/messages/count_tokens", key=key, body=BODIES[routes.COUNT_TOKENS])[0] == 200
    [record] = records(capsys)
    assert record["route"] == "/v1/messages/count_tokens"
    assert (record["input_tokens"], record["output_tokens"]) == (12, 0)
    assert (record["time_to_first_token_ms"], record["held_ms"]) == (None, 0)


def test_an_unhandled_error_is_cause_none(capsys: pytest.CaptureFixture[str]) -> None:
    text = "exception-text-b7d2"

    async def failing(request: Request, entry: routes.Route) -> Response:
        raise RuntimeError(text)

    app = build_app({routes.MODELS: failing})
    status, headers, body = call(app, "GET", "/v1/models")
    assert status == 500
    assert text.encode() not in body + b"".join(name + value for name, value in headers)
    [record] = records(capsys)
    assert (record["status"], record["cause"]) == (500, "none")
    assert text not in json.dumps(record)


@pytest.mark.parametrize("answered", [500, 503])
def test_an_answer_of_500_or_above_that_names_no_cause_is_a_fault(
    answered: int, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "exception-text-9a41"

    async def unavailable(request: Request, entry: routes.Route) -> Response:
        return Response(text, status_code=answered)

    status, headers, body = call(build_app({routes.STATUS: unavailable}), "GET", "/status")
    assert status == 500
    assert text.encode() not in body + b"".join(name + value for name, value in headers)
    [record] = records(capsys)
    assert (record["status"], record["cause"]) == (500, "none")


def test_the_cause_a_wake_answers_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    app, key = gateway()
    status, _, body = call(app, "POST", "/wake", key=key, body={"model": "evening"})
    assert status == 200
    assert json.loads(body)["cause"] == "pool_scheduled_only"
    [record] = records(capsys)
    assert (record["pool"], record["status"], record["cause"]) == ("evening", 200, "pool_scheduled_only")


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for inner in value.values() for key in _keys(inner)}
    if isinstance(value, list):
        return {key for inner in value for key in _keys(inner)}
    return set()


def test_no_embedded_metric_format(capsys: pytest.CaptureFixture[str]) -> None:
    app, key = gateway()
    for entry in routes.TABLE:
        call(app, entry.method, entry.path, key=key, body=BODIES.get(entry))
    call(app, "GET", "/v1/nowhere", key=key)
    logged = records(capsys)
    assert len(logged) == len(routes.TABLE) + 1
    assert {record["status"] for record in logged} <= {200, 404}
    # `_aws` is the key that makes a log event an embedded-metric-format one, `CloudWatchMetrics` its directive.
    for record in logged:
        assert not _keys(record) & {"_aws", "CloudWatchMetrics"}
