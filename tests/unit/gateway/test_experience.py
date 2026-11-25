"""What students experience, per pool and wire format (`rhi_gateway.experience`; system design, section 7.2).

The gateway runs whole over the in-memory key store and ledger, local routes included, with its
metrics. The engine is an `httpx.MockTransport`: its streams move the test's clock as they send
each event, and its other answers report the usage of one turn. Every series is read from the
answer of the gateway's metrics endpoint, as the collector reads it. Every student is made when the
tests run.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families
from prometheus_client.samples import Sample
from starlette.applications import Starlette
from starlette.types import Message

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.records import Session
from rhi_common.students import StudentKey
from rhi_gateway import metrics, proxy, routes
from rhi_gateway.app import Forwarding, Pools, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger
from rhi_gateway.errors import WireFormat
from rhi_gateway.experience import (
    FIRST_TOKEN,
    IN_FLIGHT,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    SERIES,
    SPEED,
    Experience,
    Turn,
)
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.readiness import PoolView

NOW = 1_800_000_000
# The course end the ledger's rows expire by.
COURSE_END_DATE = datetime.date(2027, 6, 30)
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
POOLS = parse_pools({"coursework": MOCK, "research": MOCK})
POOL = "coursework"
# The routes of a turn, one per wire format.
TURNS = {entry.wire_format: entry for entry in (routes.MESSAGES, routes.RESPONSES, routes.CHAT_COMPLETIONS)}
# One turn's usage: a stream's, and that of an answer that is not a stream.
INPUT, STREAMED, ANSWERED = 90_000, 401, 500


def in_session(pool: str, *, ready: bool = True) -> PoolView:
    """The pool's view, in a session that is active now on the clock the gateway reads, the machine's."""
    now = int(time.time())
    booked = Session(pool, instances=1, starts_at=now - 60, ends_at=now + 3600, set_by="owner", set_at=now - 60)
    return PoolView(ready=ready, session=booked)


def sse(name: str | None, data: dict[str, Any] | str) -> bytes:
    text = data if isinstance(data, str) else json.dumps(data)
    return (f"event: {name}\n" if name else "").encode() + f"data: {text}\n\n".encode()


def stream(opening: bytes, token: bytes, closing: bytes) -> list[tuple[float, bytes]]:
    """A stream and the second each part is sent at: tokens at 2, 10 and 22, between events that carry none."""
    return [(0, opening), (2, token), (10, token), (22, token), (30, closing)]


STREAMS = {
    routes.MESSAGES.upstream_path: stream(
        sse("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": INPUT}}}),
        sse("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}),
        sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": STREAMED}})
        + sse("message_stop", {"type": "message_stop"}),
    ),
    routes.RESPONSES.upstream_path: stream(
        sse("response.created", {"type": "response.created", "response": {"usage": None}}),
        sse("response.reasoning_text.delta", {"type": "response.reasoning_text.delta", "delta": "Hi"}),
        sse(
            "response.completed",
            {"type": "response.completed", "response": {"usage": {"input_tokens": INPUT, "output_tokens": STREAMED}}},
        ),
    ),
    routes.CHAT_COMPLETIONS.upstream_path: stream(
        sse(None, {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}),
        sse(None, {"choices": [{"index": 0, "delta": {"content": "Hi"}}]}),
        sse(None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        + sse(None, {"choices": [], "usage": {"prompt_tokens": INPUT, "completion_tokens": STREAMED}})
        + sse(None, "[DONE]"),
    ),
}


@dataclass
class Gateway:
    """The gateway with its metrics; the key of each student, by alias."""

    app: Starlette
    metrics: metrics.Metrics
    keys: dict[str, str]
    now: float = 0.0  # The clock the token times are read on: the engine's streams move it.
    broken: bool = False  # Whether the engine fails.
    ready: bool = True  # Whether each pool has a healthy target.
    release: asyncio.Event | None = None  # When a test gives one, the engine keeps a request until it is set.
    # The coursework pool's requests in flight, as served each time the engine had a request or sent a part of a stream.
    seen: list[float | None] = field(default_factory=list)


def student() -> tuple[str, int]:
    """A student nobody wrote down: the alias and the GitHub ID."""
    return "s" + secrets.token_hex(6), 10**17 + secrets.randbits(56)


def gateway(*students: tuple[str, int]) -> Gateway:
    keys = {alias: generate_key() for alias, _ in students}
    items = [
        StudentKey(
            key_hash=key_hash(keys[alias]),
            github_id=github_id,
            alias=alias,
            installation=None,
            issued_at=NOW - 3600,
            expires_at=NOW + 86400,
            revoked_at=None,
            concurrency_limit=4,
        )
        for alias, github_id in students
    ]
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=lambda: float(NOW))
    made = Gateway(Starlette(), metrics.Metrics(), keys)

    async def engine(request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
        made.seen.append(value(made, IN_FLIGHT, pool=POOL))
        if made.release is not None:
            await made.release.wait()
        if made.broken:
            return httpx.Response(500)
        if request.url.path == routes.COUNT_TOKENS.upstream_path:
            return httpx.Response(200, json={"input_tokens": INPUT})
        if not body.get("stream"):
            # One answer every format reads as whole, with the usage under each format's names.
            usage = {
                "input_tokens": INPUT,
                "output_tokens": ANSWERED,
                "prompt_tokens": INPUT,
                "completion_tokens": ANSWERED,
            }
            return httpx.Response(200, json={"type": "message", "usage": usage})

        async def chunks() -> AsyncIterator[bytes]:
            for at, part in STREAMS[request.url.path]:
                made.now = at
                made.seen.append(value(made, IN_FLIGHT, pool=POOL))
                yield part

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())

    def views() -> dict[str, PoolView]:
        return {pool.name: in_session(pool.name, ready=made.ready) for pool in POOLS}

    authenticator = Authenticator(InMemoryKeyStore(*items), wall_clock=lambda: float(NOW))
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
            proxy.upstream_client(httpx.MockTransport(engine)),
            "http://pools.example.com:8080",
            {pool.name: "secret" for pool in POOLS},
        ),
        pools=Pools(views=views),
    )
    made.app = build_app(
        forwarding_handlers(forwarding),
        local=LocalRoutes(authenticator=authenticator, pools=POOLS, views=views),
        metrics=made.metrics,
        pools=[pool.name for pool in POOLS],
    )
    # The series are the application's own, on the metrics' registry; only their clock is the test's.
    made.app.state.experience.clock = lambda: made.now
    return made


def post(made: Gateway, entry: routes.Route, *, stream: bool = False, alias: str | None = None) -> int:
    """One request of a student (the first, unless named) for the coursework pool: the status answered."""
    said = [{"role": "user", "content": "hello"}]
    body: dict[str, Any] = {"model": POOL, "input": "hello"} if entry == routes.RESPONSES else {"model": POOL}
    if entry != routes.RESPONSES:
        body["messages"] = said
    if entry == routes.MESSAGES:
        body["max_tokens"] = 1000
    if stream:
        body["stream"] = True
    key = made.keys[alias or next(iter(made.keys))]

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=made.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example.com") as client:
            return await client.post(entry.path, json=body, headers={"x-api-key": key})

    return asyncio.run(run()).status_code


def scrape(made: Gateway) -> str:
    """The answer of the gateway's metrics endpoint (`rhi_gateway.metrics.application`), without the socket."""
    started: list[str] = []
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": metrics.ROUTE}
    answer = metrics.application(made.metrics.registry)(environ, lambda status, headers: started.append(status))
    assert started == ["200 OK"]
    return b"".join(answer).decode()


def served(made: Gateway) -> list[Sample]:
    """Every sample of the module's series in the endpoint's answer."""
    families = text_string_to_metric_families(scrape(made))
    return [sample for family in families if family.name in SERIES for sample in family.samples]


def value(made: Gateway, name: str, **labels: str) -> float | None:
    """The value served for the one series of that name and exactly those labels; `None` when there is none."""
    found = [sample.value for sample in served(made) if sample.name == name and sample.labels == labels]
    assert len(found) <= 1
    return found[0] if found else None


@pytest.mark.parametrize("wire_format", list(WireFormat))
def test_speed_per_request(wire_format: WireFormat) -> None:
    made = gateway(student())
    assert post(made, TURNS[wire_format], stream=True) == 200

    labels = {"pool": POOL, "wire_format": wire_format.value}
    # 401 tokens, the first sent at 2 seconds and the last at 22: the 400 after the first, over 20
    # seconds, observed one step of the float above (`rhi_gateway.experience`).
    assert value(made, SPEED + "_sum", **labels) == pytest.approx(20, rel=1e-12)
    assert value(made, SPEED + "_count", **labels) == 1
    assert value(made, OUTPUT_TOKENS + "_sum", **labels) == STREAMED


@pytest.mark.parametrize(
    ("sent_at", "output_tokens"),
    [((), 401), ((2.0, 22.0), 1), ((2.0, 2.0), 401)],
    ids=["not a stream", "one token", "one moment"],
)
def test_no_speed_without_two_tokens_sent_at_two_moments(sent_at: tuple[float, ...], output_tokens: int) -> None:
    registry, now = CollectorRegistry(), [0.0]
    turn = Experience(registry, clock=lambda: now[0]).request(POOL, WireFormat.MESSAGES, first_turn=True)
    for at in sent_at:
        now[0] = at
        turn.token_sent(True)
    turn.settled(INPUT, output_tokens)

    labels = {"pool": POOL, "wire_format": "messages"}
    assert registry.get_sample_value(SPEED + "_count", labels) is None
    assert registry.get_sample_value(OUTPUT_TOKENS + "_sum", labels) == output_tokens


@pytest.mark.parametrize("wire_format", list(WireFormat))
def test_tokens_per_turn(wire_format: WireFormat) -> None:
    made = gateway(student())
    assert post(made, TURNS[wire_format]) == 200

    labels = {"pool": POOL, "wire_format": wire_format.value}
    assert value(made, INPUT_TOKENS + "_sum", **labels) == 90_000
    assert value(made, OUTPUT_TOKENS + "_sum", **labels) == 500
    assert value(made, INPUT_TOKENS + "_count", **labels) == value(made, OUTPUT_TOKENS + "_count", **labels) == 1
    # An answer that is not a stream has no speed, and no other pool or format has this turn.
    assert {sample.labels["wire_format"] for sample in served(made) if "wire_format" in sample.labels} == {wire_format}
    assert value(made, SPEED + "_count", **labels) is None


def test_every_request_that_settles_is_observed_once() -> None:
    made = gateway(student())
    labels = {"pool": POOL, "wire_format": "messages"}
    # A token count settles its input and no output.
    assert post(made, routes.COUNT_TOKENS) == 200
    assert (value(made, INPUT_TOKENS + "_sum", **labels), value(made, OUTPUT_TOKENS + "_sum", **labels)) == (INPUT, 0)
    # A request whose engine fails settles with nothing observed: it is counted, at zero.
    made.broken = True
    assert post(made, routes.MESSAGES) == 502
    assert value(made, INPUT_TOKENS + "_count", **labels) == value(made, OUTPUT_TOKENS + "_count", **labels) == 2
    assert value(made, INPUT_TOKENS + "_bucket", le="0.0", **labels) == 1
    assert value(made, INPUT_TOKENS + "_sum", **labels) == INPUT


def in_flight(made: Gateway) -> dict[str, float | None]:
    """The requests in flight the endpoint serves for each configured pool."""
    return {pool.name: value(made, IN_FLIGHT, pool=pool.name) for pool in POOLS}


def test_a_request_counts_in_flight_until_it_ends_once() -> None:
    registry = CollectorRegistry()
    made = Experience(registry, [POOL, "research"])

    def counts() -> dict[str, float | None]:
        return {name: registry.get_sample_value(IN_FLIGHT, {"pool": name}) for name in (POOL, "research")}

    # Every configured pool has its series, zero for one with no request, read at each scrape.
    first, second = made.forwarded(POOL), made.forwarded(POOL)
    assert counts() == {POOL: 2, "research": 0}
    first.end()
    first.end()
    assert counts() == {POOL: 1, "research": 0}
    second.end()
    assert counts() == {POOL: 0, "research": 0}


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "not a stream"])
def test_a_forwarded_request_is_in_flight_from_its_forwarding_to_its_end(stream: bool) -> None:
    made = gateway(student())
    assert in_flight(made) == {POOL: 0, "research": 0}
    assert post(made, routes.MESSAGES, stream=stream) == 200
    # In flight while the engine had it and, for a stream, as each part was sent; not once it has ended.
    assert len(made.seen) == (1 + len(STREAMS[routes.MESSAGES.upstream_path]) if stream else 1)
    assert set(made.seen) == {1}
    assert in_flight(made) == {POOL: 0, "research": 0}


def test_a_token_count_and_the_local_routes_are_not_in_flight() -> None:
    made = gateway(student())
    (key,) = made.keys.values()

    async def local() -> list[int]:
        transport = httpx.ASGITransport(app=made.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example.com") as client:
            entries = (routes.STATUS, routes.MODELS, routes.HELLO)
            answers = [await client.request(entry.method, entry.path, headers={"x-api-key": key}) for entry in entries]
            return [answer.status_code for answer in answers]

    assert asyncio.run(local()) == [200, 200, 200]
    assert post(made, routes.COUNT_TOKENS) == 200
    # Only the token count reached the engine, and it was not in flight while it was there.
    assert made.seen == [0]
    assert in_flight(made) == {POOL: 0, "research": 0}


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "not a stream"])
def test_a_request_the_pool_cannot_serve_is_never_in_flight(stream: bool) -> None:
    made = gateway(student())
    made.ready = False  # Its session is active, and no target is healthy.
    assert post(made, routes.MESSAGES, stream=stream) == 503
    assert (made.seen, in_flight(made)) == ([], {POOL: 0, "research": 0})


def test_a_request_the_client_leaves_is_no_longer_in_flight() -> None:
    made = gateway(student())
    (key,) = made.keys.values()
    body = json.dumps({"model": POOL, "max_tokens": 10, "messages": [{"role": "user", "content": "hello"}]}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": routes.MESSAGES.path,
        "raw_path": routes.MESSAGES.path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"x-api-key", key.encode()), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("gateway.example.com", 443),
    }

    async def scenario() -> None:
        made.release = asyncio.Event()  # Never set: the engine keeps the request.
        request: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]
        left = asyncio.Event()

        async def receive() -> Message:
            if request:
                return request.pop()
            await left.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            pass

        call = asyncio.ensure_future(made.app(scope, receive, send))
        # A guard, not the clock under test.
        async with asyncio.timeout(10):
            while not made.seen:
                await asyncio.sleep(0.001)
            assert in_flight(made)[POOL] == 1
            left.set()
            await call
        assert in_flight(made) == {POOL: 0, "research": 0}

    asyncio.run(scenario())


def test_no_student_label() -> None:
    students = [student(), student()]
    made = gateway(*students)
    for alias, _ in students:
        for entry in (*TURNS.values(), routes.COUNT_TOKENS):
            assert post(made, entry, alias=alias, stream=entry != routes.COUNT_TOKENS) == 200
            assert post(made, entry, alias=alias) == 200

    samples = served(made)
    # Every series of the module is there. Its labels are the pool, the wire format and a histogram's
    # boundary, and for the two waits which turn it is.
    assert all(any(sample.name.startswith(name) for sample in samples) for name in SERIES)
    assert {label for sample in samples for label in sample.labels} == {"pool", "wire_format", "le", "turn"}
    # The gateway holds no request: nothing it serves says how a request started, and it has no
    # gauge of requests held.
    everything = list(text_string_to_metric_families(scrape(made)))
    assert not [sample for family in everything for sample in family.samples if "start" in sample.labels]
    assert not [family.name for family in everything if "held" in family.name]
    names = [str(name) for alias, github_id in students for name in (alias, github_id, made.keys[alias])]
    for sample in samples:
        written = " ".join([sample.name, *sample.labels, *sample.labels.values()])
        assert not any(name in written for name in names)
    # The same answer does name the two students, by alias, in the series per student.
    assert all(f'alias="{alias}"' in scrape(made) for alias, _ in students)


def test_served_with_the_gateway_metrics_with_the_floors_as_boundaries() -> None:
    made = gateway(student())
    assert post(made, routes.MESSAGES, stream=True) == 200

    families = {family.name for family in text_string_to_metric_families(scrape(made))}
    assert families >= {*SERIES, metrics.REQUESTS}
    boundaries = {float(sample.labels["le"]) for sample in served(made) if sample.name == SPEED + "_bucket"}
    # The floors of the objectives, 10 and 15 tokens per second, are read exactly.
    assert boundaries >= {10.0, 15.0}


def test_a_value_at_a_floor_is_not_in_the_floors_bucket() -> None:
    # A first token at exactly 5 seconds is not under 5; a stream at exactly 15 tokens per second is
    # at 15 or more. The bucket of each floor holds neither, the next one up holds each.
    registry, now = CollectorRegistry(), [0.0]
    one = Turn(Experience(registry, clock=lambda: now[0]), POOL, WireFormat.MESSAGES, first_turn=True)
    now[0] = 5.0
    one.token_sent(answer=True)
    now[0] = 25.0
    one.token_sent(answer=True)
    one.settled(input_tokens=10, output_tokens=301)
    wait = {"pool": POOL, "wire_format": "messages", "turn": "first"}
    speed = {"pool": POOL, "wire_format": "messages"}
    for name, labels, floor, above in ((FIRST_TOKEN, wait, "5.0", "7.5"), (SPEED, speed, "15.0", "17.5")):
        assert registry.get_sample_value(f"{name}_bucket", {**labels, "le": floor}) == 0
        assert registry.get_sample_value(f"{name}_bucket", {**labels, "le": above}) == 1
