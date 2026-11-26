"""An engine's refusal of a prompt longer than the pool's context (`rhi_gateway.proxy`; system design, section 6.2, step 8).

vLLM refuses such a prompt with a 400 whose error message holds "maximum context length". The
gateway answers it 400 `context_length_exceeded`, in the route's wire format, on which Claude Code
compacts its session; every other answer of the engine that is not a success stays 502
`engine_error`. None of the engine's text reaches the client, and the request is settled once.

The gateway runs whole over the in-memory key store and a ledger that counts its settlements. The
upstream is an `httpx.MockTransport` handler that answers the bodies vLLM 0.33.0 sends, as they
are. The time limit is a deadline the test reaches itself.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.records import Session
from rhi_common.students import StudentKey, utc_day
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, Pools, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger, Reservation
from rhi_gateway.errors import WireFormat, render_json
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.readiness import PoolView

NOW = 1_800_000_000
STUDENT = 4242
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
SAID = [{"role": "user", "content": "hello"}]
BODIES: dict[routes.Route, dict[str, Any]] = {
    routes.MESSAGES: {"model": "coursework", "max_tokens": 100, "messages": SAID},
    routes.COUNT_TOKENS: {"model": "coursework", "messages": SAID},
    routes.CHAT_COMPLETIONS: {"model": "coursework", "messages": SAID},
    routes.RESPONSES: {"model": "coursework", "input": "hello"},
}
ROUTES = pytest.mark.parametrize("entry", list(BODIES), ids=lambda entry: entry.path)

# vLLM 0.33.0's refusal, as it sends it: on the Messages routes, then on the OpenAI ones.
REFUSAL = (
    "This model's maximum context length is 262144 tokens. However, your prompt contains at least 262145 input"
    " tokens, which exceeds this limit even without any output tokens. Please reduce the length of the input"
    " prompt. (parameter=input_tokens, value=262145)"
)
OTHER = "Expected 'role' to be one of 'user' or 'assistant'."
ENGINE_HEADER = "engine-internal-detail-7f3a"


def vllm_error(entry: routes.Route, message: str) -> dict[str, Any]:
    if entry.wire_format is WireFormat.MESSAGES:
        return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return {"error": {"message": message, "type": "BadRequestError", "param": "input_tokens", "code": 400}}


class Ledger(InMemoryLedger):
    """The in-memory ledger, counting the settlements made."""

    def __init__(self) -> None:
        super().__init__(course_end=datetime.date(2027, 6, 30), clock=lambda: float(NOW))
        self.settlements = 0

    def settle(self, reservation: Reservation, **named: Any) -> bool:
        self.settlements += 1
        return super().settle(reservation, **named)

    def leases(self) -> dict[str, Any]:
        day = self.day(STUDENT, utc_day(NOW))
        return {} if day is None else dict(day.leases)


class Deadline:
    """The request's time limit, reached only when the test says so."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        await self.reached.wait()


async def ask(
    entry: routes.Route, status: int, parts: list[bytes], *, stall: bool = False, reach_limit: bool = False
) -> tuple[httpx.Response, Ledger]:
    """One request through the gateway to an engine that answers `status` with `parts`, and may then stall."""
    key = generate_key()
    student = StudentKey(
        key_hash=key_hash(key),
        github_id=STUDENT,
        alias="otter",
        installation=None,
        issued_at=NOW - 3600,
        expires_at=NOW + 86400,
        revoked_at=None,
        concurrency_limit=4,
    )
    ledger = Ledger()
    deadline = Deadline()
    sent = asyncio.Event()

    async def engine(request: httpx.Request) -> httpx.Response:
        await request.aread()

        async def body() -> AsyncIterator[bytes]:
            for part in parts:
                yield part
            sent.set()
            if stall:
                await asyncio.get_running_loop().create_future()

        headers = {"content-type": "application/json", "x-engine-detail": ENGINE_HEADER}
        return httpx.Response(status, headers=headers, content=body())

    booked = Session("coursework", instances=1, starts_at=NOW - 60, ends_at=NOW + 3600, set_by="owner", set_at=NOW)
    forwarding = Forwarding(
        authenticator=Authenticator(InMemoryKeyStore(student), wall_clock=lambda: float(NOW)),
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
            {"coursework": "secret"},
            sleep=deadline.sleep,
        ),
        pools=Pools(
            views=lambda: {"coursework": PoolView(ready=True, session=booked)},
            clock=lambda: float(NOW),
        ),
    )
    app = build_app(forwarding_handlers(forwarding))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example.com") as client:
        answer = asyncio.ensure_future(client.post(entry.path, json=BODIES[entry], headers={"x-api-key": key}))
        if reach_limit:
            # Once the engine has sent its start, or the gateway has answered without waiting for it.
            waiting = asyncio.ensure_future(sent.wait())
            await asyncio.wait({waiting, answer}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
            deadline.reached.set()
        return await asyncio.wait_for(answer, timeout=10), ledger


def call(entry: routes.Route, status: int, parts: list[bytes], **named: bool) -> tuple[httpx.Response, Ledger]:
    return asyncio.run(ask(entry, status, parts, **named))


def assert_answered(answer: httpx.Response, ledger: Ledger, entry: routes.Route, cause: Cause) -> None:
    """The cause's own answer in the route's format, nothing of the engine's in it, and one settlement."""
    assert isinstance(entry.wire_format, WireFormat)
    expected = render_json(cause, entry.wire_format)
    assert (answer.status_code, answer.content) == (expected.status, expected.body)
    sent = answer.content + b"".join(name + value for name, value in answer.headers.raw)
    for engine_text in (REFUSAL, OTHER, ENGINE_HEADER, "262144", "invalid_request_error", "BadRequestError"):
        assert engine_text.encode() not in sent
    assert ledger.settlements == 1
    assert ledger.leases() == {}


@ROUTES
def test_an_engine_refusal_of_a_prompt_too_long_is_context_length_exceeded(entry: routes.Route) -> None:
    body = json.dumps(vllm_error(entry, REFUSAL)).encode()
    # The engine's body in two pieces, cut in its message: it is read whole before it is judged.
    answer, ledger = call(entry, 400, [body[:60], body[60:]])
    assert answer.status_code == 400
    assert_answered(answer, ledger, entry, Cause.CONTEXT_LENGTH_EXCEEDED)


@pytest.mark.parametrize(("status", "message"), [(400, OTHER), (500, REFUSAL)], ids=["other 400", "500"])
@ROUTES
def test_every_other_failure_of_the_engine_is_engine_error(entry: routes.Route, status: int, message: str) -> None:
    answer, ledger = call(entry, status, [json.dumps(vllm_error(entry, message)).encode()])
    assert answer.status_code == 502
    assert_answered(answer, ledger, entry, Cause.ENGINE_ERROR)


@ROUTES
def test_a_refusal_is_read_within_the_time_limit_and_another_failure_is_not_read(entry: routes.Route) -> None:
    start = json.dumps(vllm_error(entry, REFUSAL)).encode()[:60]
    # A 400 whose body stalls ends at the request's time limit.
    answer, ledger = call(entry, 400, [start], stall=True, reach_limit=True)
    assert_answered(answer, ledger, entry, Cause.TIME_LIMIT)
    # A 500 whose body stalls is answered at once: its body is not waited for.
    answer, ledger = call(entry, 500, [start], stall=True)
    assert_answered(answer, ledger, entry, Cause.ENGINE_ERROR)
