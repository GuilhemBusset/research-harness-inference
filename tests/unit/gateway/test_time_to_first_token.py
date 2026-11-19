"""The time to the first token and to the first answer of a stream (`rhi_gateway.experience`; system design, section 7.2).

The gateway runs whole (application, proxy, experience) over the in-memory key store and ledger. The
engine is an `httpx.MockTransport` whose stream moves the test's clock as it sends each part: a
request is accepted when that clock reads 0, and a part written to the client at 12 was waited for
12 seconds. Both histograms are read from the answer of the gateway's metrics endpoint. Every
student is made when the tests run.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
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
from rhi_gateway.experience import FIRST_ANSWER, FIRST_TOKEN
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.readiness import PoolView

NOW = 1_800_000_000
POOL = "coursework"
POOLS = parse_pools(
    {
        POOL: {
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
# The routes of a turn, one per wire format.
TURNS = {entry.wire_format: entry for entry in (routes.MESSAGES, routes.RESPONSES, routes.CHAT_COMPLETIONS)}

MESSAGES, RESPONSES, CHAT = WireFormat.MESSAGES, WireFormat.RESPONSES, WireFormat.CHAT_COMPLETIONS


def sse(name: str | None, data: dict[str, Any] | str) -> bytes:
    text = data if isinstance(data, str) else json.dumps(data)
    return (f"event: {name}\n" if name else "").encode() + f"data: {text}\n\n".encode()


def block(piece: dict[str, Any]) -> bytes:
    return sse("content_block_delta", {"type": "content_block_delta", "delta": piece})


def item(kind: str, **fields: Any) -> bytes:
    return sse(kind, {"type": kind, **fields})


def chunk(delta: dict[str, Any], **choice: Any) -> bytes:
    return sse(None, {"choices": [{"index": 0, "delta": delta, **choice}]})


# What an engine sends, per wire format. `opening` and `named` carry no token: the role, and a tool
# call's name before any of its arguments. The Chat Completions reasoning is in a field the engine
# adds: the format's reference has none.
EVENTS: dict[WireFormat, dict[str, bytes]] = {
    MESSAGES: {
        "opening": sse("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 900}}}),
        "reasoning": block({"type": "thinking_delta", "thinking": "Hm"}),
        "text": block({"type": "text_delta", "text": "Hi"}),
        "named": sse("content_block_start", {"content_block": {"type": "tool_use", "name": "read", "input": {}}}),
        "arguments": block({"type": "input_json_delta", "partial_json": '{"path"'}),
        "closing": sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 40}})
        + sse("message_stop", {"type": "message_stop"}),
    },
    RESPONSES: {
        "opening": item("response.created", response={"usage": None}),
        "reasoning": item("response.reasoning_text.delta", delta="Hm"),
        "text": item("response.output_text.delta", delta="Hi"),
        "named": item("response.output_item.added", item={"type": "function_call", "name": "read", "arguments": ""}),
        "arguments": item("response.function_call_arguments.delta", delta='{"path"'),
        "closing": item("response.completed", response={"usage": {"input_tokens": 900, "output_tokens": 40}}),
    },
    CHAT: {
        "opening": chunk({"role": "assistant", "content": ""}),
        "reasoning": chunk({"reasoning_content": "Hm"}),
        "text": chunk({"content": "Hi"}),
        "named": chunk({"tool_calls": [{"index": 0, "function": {"name": "read", "arguments": ""}}]}),
        "arguments": chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"path"'}}]}),
        "closing": chunk({}, finish_reason="stop")
        + sse(None, {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 40}})
        + sse(None, "[DONE]"),
    },
}

HELLO = {"role": "user", "content": "hello"}
# A conversation that carries an earlier answer of the model, as each format writes it.
ANSWERED = {
    MESSAGES: [HELLO, {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]}, HELLO],
    CHAT: [HELLO, {"role": "assistant", "content": "Hi"}, HELLO],
    RESPONSES: [
        HELLO,
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi"}]},
        HELLO,
    ],
}
# Codex's turn after a tool call: the model's earlier output is its call, with no message of its own.
CALLED = [
    HELLO,
    {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{}"},
    {"type": "function_call_output", "call_id": "call_1", "output": "done"},
]


@dataclass
class Gateway:
    """The gateway with its metrics; the key of each student, by alias."""

    app: Starlette
    metrics: metrics.Metrics
    keys: dict[str, str]
    now: float = 0.0  # The clock the waits are read on: 0 when a request arrives, then moved by the engine's stream.
    ready: bool = True  # Whether the pool is: a request for one that is not is held.
    sends: list[tuple[float, bytes]] = field(default_factory=list)  # The engine's stream, and when each part is sent.


def student() -> tuple[str, int]:
    """A student nobody wrote down: the alias and the GitHub ID."""
    return "s" + secrets.token_hex(6), 10**17 + secrets.randbits(56)


def gateway(*students: tuple[str, int]) -> Gateway:
    students = students or (student(),)
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
    ledger = InMemoryLedger(clock=lambda: float(NOW))
    made = Gateway(Starlette(), metrics.Metrics(ledger), keys)

    async def engine(request: httpx.Request) -> httpx.Response:
        if not json.loads(await request.aread()).get("stream"):
            # One answer every format reads as whole.
            usage = {"input_tokens": 900, "output_tokens": 40, "prompt_tokens": 900, "completion_tokens": 40}
            return httpx.Response(200, json={"type": "message", "usage": usage})

        async def parts() -> AsyncIterator[bytes]:
            for at, part in made.sends:
                made.now = at
                yield part

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=parts())

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
            {POOL: "secret"},
        ),
        holding=Holding(views=lambda: {POOL: PoolView(ready=made.ready)}, wake=lambda pool: None, sleep=sleep),
    )
    made.app = build_app(forwarding_handlers(forwarding), metrics=made.metrics)
    # The series are the application's own, on the metrics' registry; only their clock is the test's.
    made.app.state.experience.clock = lambda: made.now
    return made


def turn(
    made: Gateway,
    wire_format: WireFormat,
    *sends: tuple[float, str],
    said: list[dict[str, Any]] | None = None,
    held: bool = False,
    stream: bool = True,
    alias: str | None = None,
) -> None:
    """One request, accepted at 0 seconds, whose engine sends each named event at its second, between an opening and a closing."""
    events = EVENTS[wire_format]
    last = max((at for at, _ in sends), default=0.0)
    made.sends = [(0.0, events["opening"]), *((at, events[name]) for at, name in sends), (last + 1, events["closing"])]
    made.now, made.ready = 0.0, not held
    body: dict[str, Any] = {"model": POOL, "stream": stream}
    body["input" if wire_format is RESPONSES else "messages"] = [HELLO] if said is None else said
    if wire_format is MESSAGES:
        body["max_tokens"] = 1000
    key = made.keys[alias or next(iter(made.keys))]

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=made.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example.com") as client:
            return await client.post(TURNS[wire_format].path, json=body, headers={"x-api-key": key})

    assert asyncio.run(run()).status_code == 200


def served(made: Gateway, name: str) -> list[Sample]:
    """Every sample of one histogram in the answer of the gateway's metrics endpoint, read without the socket."""
    started: list[str] = []
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": metrics.ROUTE}
    answer = metrics.application(made.metrics.registry)(environ, lambda status, headers: started.append(status))
    assert started == ["200 OK"]
    families = text_string_to_metric_families(b"".join(answer).decode())
    return [sample for family in families if family.name == name for sample in family.samples]


def waits(made: Gateway, name: str) -> dict[tuple[str, str, str], list[float]]:
    """What one histogram holds: the requests and the sum of their seconds, for each series it serves.

    A series is named by its wire format, `warm` or `held`, and `first` or `later`.
    """
    found: dict[tuple[str, str, str], list[float]] = {}
    for sample in served(made, name):
        labels = sample.labels
        assert labels["pool"] == POOL
        series = found.setdefault((labels["wire_format"], labels["start"], labels["turn"]), [0.0, 0.0])
        if sample.name == name + "_count":
            series[0] = sample.value
        elif sample.name == name + "_sum":
            series[1] = sample.value
    return found


def test_held_stream_waits_for_its_first_token_then_its_first_answer() -> None:
    made = gateway()
    turn(made, MESSAGES, (12, "reasoning"), (20, "text"), (25, "text"), held=True)

    # One request in each histogram, under `held` and under nothing else: 12 seconds, and 20.
    assert waits(made, FIRST_TOKEN) == {("messages", "held", "first"): [1, 12]}
    assert waits(made, FIRST_ANSWER) == {("messages", "held", "first"): [1, 20]}


@pytest.mark.parametrize(
    ("wire_format", "earlier"),
    [(MESSAGES, ANSWERED[MESSAGES]), (CHAT, ANSWERED[CHAT]), (RESPONSES, ANSWERED[RESPONSES]), (RESPONSES, CALLED)],
    ids=["messages", "chat completions", "responses", "responses after a tool call"],
)
def test_first_turn_or_later(wire_format: WireFormat, earlier: list[dict[str, Any]]) -> None:
    made = gateway()
    turn(made, wire_format, (2, "text"), said=[HELLO])
    assert waits(made, FIRST_TOKEN) == waits(made, FIRST_ANSWER) == {(wire_format.value, "warm", "first"): [1, 2]}

    turn(made, wire_format, (7, "text"), said=earlier)
    expected = {(wire_format.value, "warm", "first"): [1, 2], (wire_format.value, "warm", "later"): [1, 7]}
    assert waits(made, FIRST_TOKEN) == waits(made, FIRST_ANSWER) == expected


def test_chat_reasoning_deltas() -> None:
    made = gateway()
    # The delta at 3 seconds has text in a field of the engine's: neither `content` nor a tool call.
    turn(made, CHAT, (3, "reasoning"), (5, "reasoning"), (9, "text"), (11, "text"))

    assert waits(made, FIRST_TOKEN) == {("chat_completions", "warm", "first"): [1, 3]}
    assert waits(made, FIRST_ANSWER) == {("chat_completions", "warm", "first"): [1, 9]}


@pytest.mark.parametrize("answer", ["text", "arguments"], ids=["text", "tool call"])
@pytest.mark.parametrize("wire_format", list(WireFormat))
def test_first_answer_is_the_first_text_or_tool_call_after_the_reasoning(wire_format: WireFormat, answer: str) -> None:
    made = gateway()
    # A tool call's name, sent at 6 before any of its arguments, is no token: the answer begins at 7.
    turn(made, wire_format, (4, "reasoning"), (5, "reasoning"), (6, "named"), (7, answer), (8, answer))

    assert waits(made, FIRST_TOKEN) == {(wire_format.value, "warm", "first"): [1, 4]}
    assert waits(made, FIRST_ANSWER) == {(wire_format.value, "warm", "first"): [1, 7]}


@pytest.mark.parametrize("wire_format", list(WireFormat))
def test_only_what_a_stream_sent_is_waited_for(wire_format: WireFormat) -> None:
    made = gateway()
    # An answer that is not a stream has neither wait.
    turn(made, wire_format, stream=False)
    assert waits(made, FIRST_TOKEN) == waits(made, FIRST_ANSWER) == {}
    # A stream that only reasoned sent a first token, and no answer.
    turn(made, wire_format, (4, "reasoning"), (6, "reasoning"))
    assert waits(made, FIRST_TOKEN) == {(wire_format.value, "warm", "first"): [1, 4]}
    assert waits(made, FIRST_ANSWER) == {}


def test_five_seconds_is_a_boundary_and_no_label_is_a_student() -> None:
    students = [student(), student()]
    made = gateway(*students)
    for alias, _ in students:
        for wire_format in WireFormat:
            turn(made, wire_format, (4, "reasoning"), (6, "text"), alias=alias)
            turn(made, wire_format, (4, "reasoning"), (6, "text"), alias=alias, held=True, said=ANSWERED[wire_format])

    names = [str(name) for alias, github_id in students for name in (alias, github_id, made.keys[alias])]
    for histogram in (FIRST_TOKEN, FIRST_ANSWER):
        samples = served(made, histogram)
        assert {label for sample in samples for label in sample.labels} == {
            "pool",
            "wire_format",
            "start",
            "turn",
            "le",
        }
        for sample in samples:
            assert not any(name in " ".join([sample.name, *sample.labels.values()]) for name in names)
        # The objective's 5 seconds is read exactly. Each warm stream sent its first token at 4 seconds
        # and its first answer at 6: the two students' are within the boundary in one histogram only.
        at_five = [
            sample.value
            for sample in samples
            if sample.name == histogram + "_bucket"
            and sample.labels["le"] == "5.0"
            and sample.labels["start"] == "warm"
        ]
        assert at_five == [2 if histogram == FIRST_TOKEN else 0] * len(WireFormat)
