"""What students experience, per pool and wire format (`rhi_gateway.experience`; system design, section 7.2).

The gateway runs whole over the in-memory key store and ledger, with a heartbeat and its metrics.
The engine is an `httpx.MockTransport`: its streams move the test's clock as they send each event,
and its other answers report the usage of one turn. Every series is read from the answer of the
gateway's metrics endpoint, as the collector reads it. Every student is made when the tests run.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families
from prometheus_client.samples import Sample
from starlette.applications import Starlette

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.students import StudentKey
from rhi_gateway import metrics, proxy, routes
from rhi_gateway.app import Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger
from rhi_gateway.errors import WireFormat
from rhi_gateway.experience import HELD, IN_FLIGHT, INPUT_TOKENS, OUTPUT_TOKENS, SERIES, SPEED, Experience
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.heartbeat import Heartbeat
from rhi_gateway.keystore import InMemoryKeyStore
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


class Table:
    """The heartbeats table: nothing is written here, the rows are read as they would be."""

    def put_item(self, Item: dict[str, Any]) -> None:
        pass


@dataclass
class Gateway:
    """The gateway with its heartbeat and its metrics; the key of each student, by alias."""

    app: Starlette
    metrics: metrics.Metrics
    heartbeat: Heartbeat
    keys: dict[str, str]
    now: float = 0.0  # The clock the token times are read on: the engine's streams move it.
    ready: bool = True  # Whether the pools are: a request for one that is not is held.
    broken: bool = False  # Whether the engine fails.


def student() -> tuple[str, int]:
    """A student nobody wrote down: the alias and the GitHub ID."""
    return "s" + secrets.token_hex(6), 10**17 + secrets.randbits(56)


def gateway(*students: tuple[str, int], ready: bool = True) -> Gateway:
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
            daily_token_budget=10_000_000,
            concurrency_limit=4,
        )
        for alias, github_id in students
    ]
    ledger = InMemoryLedger(course_end=COURSE_END_DATE, clock=lambda: float(NOW))
    heartbeat = Heartbeat(Table(), "task", [pool.name for pool in POOLS], clock=lambda: float(NOW))
    made = Gateway(Starlette(), metrics.Metrics(ledger), heartbeat, keys, ready=ready)

    async def engine(request: httpx.Request) -> httpx.Response:
        body = json.loads(await request.aread())
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
                yield part

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())

    async def sleep(seconds: float) -> None:
        # The pool turns ready during the first wait of a hold; a later wait is given up when the engine answers.
        turned, made.ready = made.ready, True
        await asyncio.sleep(seconds if turned else 0)

    forwarding = Forwarding(
        authenticator=Authenticator(InMemoryKeyStore(*items), wall_clock=lambda: float(NOW)),
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
        holding=Holding(
            views=lambda: {pool.name: PoolView(ready=made.ready) for pool in POOLS},
            wake=lambda pool: None,
            sleep=sleep,
        ),
        heartbeat=heartbeat,
    )
    made.app = build_app(forwarding_handlers(forwarding), heartbeat=heartbeat, metrics=made.metrics)
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


@pytest.mark.parametrize("held", [False, True], ids=["warm", "held"])
@pytest.mark.parametrize("wire_format", list(WireFormat))
def test_speed_per_request(wire_format: WireFormat, held: bool) -> None:
    made = gateway(student(), ready=not held)
    assert post(made, TURNS[wire_format], stream=True) == 200

    labels = {"pool": POOL, "wire_format": wire_format.value}
    # 401 tokens, the first sent at 2 seconds and the last at 22: the 400 after the first, over 20 seconds.
    assert value(made, SPEED + "_sum", **labels) == 20
    assert value(made, SPEED + "_count", **labels) == 1
    assert value(made, OUTPUT_TOKENS + "_sum", **labels) == STREAMED


@pytest.mark.parametrize(
    ("sent_at", "output_tokens"),
    [((), 401), ((2.0, 22.0), 1), ((2.0, 2.0), 401)],
    ids=["not a stream", "one token", "one moment"],
)
def test_no_speed_without_two_tokens_sent_at_two_moments(sent_at: tuple[float, ...], output_tokens: int) -> None:
    registry, now = CollectorRegistry(), [0.0]
    turn = Experience(registry, clock=lambda: now[0]).request(POOL, WireFormat.MESSAGES, held=False, first_turn=True)
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


def test_in_flight_and_held_match_the_heartbeat() -> None:
    made = gateway(student())
    first = made.heartbeat.received(POOL, held=False)
    made.heartbeat.received(POOL, held=False)
    waiting = made.heartbeat.received(POOL, held=True)

    def reported() -> dict[str, tuple[float, float]]:
        return {row.pool: (row.in_flight, row.held) for row in made.heartbeat.rows()}

    def served_counts() -> dict[str, tuple[float | None, float | None]]:
        return {
            pool.name: (value(made, IN_FLIGHT, pool=pool.name), value(made, HELD, pool=pool.name)) for pool in POOLS
        }

    assert served_counts() == reported() == {POOL: (2, 1), "research": (0, 0)}
    # They are read at each scrape: the held request is forwarded, and one in flight ends.
    waiting.forwarded()
    first.end()
    assert served_counts() == reported() == {POOL: (2, 0), "research": (0, 0)}


def test_no_student_label() -> None:
    students = [student(), student()]
    made = gateway(*students)
    for alias, _ in students:
        for entry in (*TURNS.values(), routes.COUNT_TOKENS):
            assert post(made, entry, alias=alias, stream=entry != routes.COUNT_TOKENS) == 200
            assert post(made, entry, alias=alias) == 200

    samples = served(made)
    # Every series of the module is there. Its labels are the pool, the wire format and a histogram's
    # boundary, and for the two waits how the request started and which turn it is.
    assert all(any(sample.name.startswith(name) for sample in samples) for name in SERIES)
    assert {label for sample in samples for label in sample.labels} == {"pool", "wire_format", "le", "start", "turn"}
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
    assert families >= {*SERIES, metrics.REQUESTS, metrics.BUDGET_LEFT}
    boundaries = {float(sample.labels["le"]) for sample in served(made) if sample.name == SPEED + "_bucket"}
    # The floors of the objectives, 10 and 15 tokens per second, are read exactly.
    assert boundaries >= {10.0, 15.0}
