"""A pool that cannot serve yet answers at once (`rhi_gateway.app`; system design, sections 5.3 and 6.2, step 5).

A request never starts a machine, so the gateway holds none. A request for a pool whose session is
active and that has no healthy target is answered 503 in its wire format, before a slot is taken:
`warming` when the scaler's note counts an instance starting, `capacity_unavailable` when it
counts none. A token count gets the answer a request would get.

The gateway runs whole over what it would keep of the pool: a session that is active, no healthy
target, and the note the test gives. The ledger and the engine record what reaches them. Every key
is generated when the test runs.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.records import ScalerNote, Session
from rhi_common.students import StudentKey
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, Pools, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger, Reservation
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.readiness import PoolView

NOW = 1_800_000_000
POOLS = parse_pools(
    {
        "coursework": {
            "engine": "mock",
            "instance_types": ["t3.small"],
            "prices": {"t3.small": 0.0208},
            "max_instances": 2,
            "warm_instances": 0,
            "wake": "on-demand",
            "target_concurrency": 8,
            "context_length": 131072,
            "largest_output": 8000,
        }
    }
)
SAID = [{"role": "user", "content": "hello"}]
# A request of each wire format, and the token count.
BODIES: dict[routes.Route, dict[str, Any]] = {
    routes.MESSAGES: {"model": "coursework", "max_tokens": 100, "messages": SAID},
    routes.CHAT_COMPLETIONS: {"model": "coursework", "messages": SAID},
    routes.RESPONSES: {"model": "coursework", "input": "hello"},
    routes.COUNT_TOKENS: {"model": "coursework", "messages": SAID},
}
FORMATS = pytest.mark.parametrize(
    "entry", [routes.MESSAGES, routes.CHAT_COMPLETIONS, routes.RESPONSES], ids=lambda entry: entry.path
)
STREAMS = pytest.mark.parametrize("stream", [True, False], ids=["a stream", "not a stream"])


def note(starting: int) -> ScalerNote:
    """The scaler's note of a pool it asked two instances for, this many of them starting."""
    return ScalerNote(
        "coursework", capacity_asked=2, no_capacity_at=None, written_at=NOW, requested=2, starting=starting
    )


# What the gateway may keep of a pool with no healthy target, and the cause each gives.
STARTING = pytest.param(note(1), Cause.WARMING, id="one starting")
NONE_STARTING = [
    pytest.param(note(0), Cause.CAPACITY_UNAVAILABLE, id="none starting"),
    # A note written before the scaler counted them holds no count, and reads as none starting.
    pytest.param(
        ScalerNote.from_item({"pool": "coursework", "record": "note", "capacity_asked": 2, "written_at": NOW}),
        Cause.CAPACITY_UNAVAILABLE,
        id="a note without the count",
    ),
    pytest.param(None, Cause.CAPACITY_UNAVAILABLE, id="no note"),
]


class Ledger(InMemoryLedger):
    """The ledger, which records each slot asked of it."""

    def __init__(self) -> None:
        super().__init__(course_end=datetime.date(2027, 6, 30), clock=lambda: float(NOW))
        self.reserved: list[str] = []

    def reserve(self, student: Any, request_id: str, **rest: Any) -> Reservation:
        self.reserved.append(request_id)
        return super().reserve(student, request_id, **rest)


class Gateway:
    """The gateway over one pool in a session, with the view the test gives it and its own key."""

    def __init__(self, view: PoolView, retry_after: Mapping[Cause, int] | None = None) -> None:
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
        self.ledger = Ledger()
        self.reached: list[httpx.Request] = []

        def engine(request: httpx.Request) -> httpx.Response:
            self.reached.append(request)
            # One answer every forwarded route reads as whole, with the usage under each format's names.
            usage = {"input_tokens": 3, "output_tokens": 2, "prompt_tokens": 3, "completion_tokens": 2}
            return httpx.Response(200, json={"type": "message", "input_tokens": 3, "usage": usage})

        booked = Session("coursework", instances=2, starts_at=NOW - 60, ends_at=NOW + 3600, set_by="owner", set_at=NOW)
        forwarding = Forwarding(
            authenticator=Authenticator(InMemoryKeyStore(student), wall_clock=lambda: float(NOW)),
            ledger=self.ledger,
            filters={
                routes.MESSAGES: BodyFilter(messages.MESSAGES, POOLS),
                routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, POOLS),
                routes.CHAT_COMPLETIONS: ChatBodyFilter(POOLS),
                routes.RESPONSES: BodyFilter(responses.RESPONSES, POOLS),
            },
            proxy=proxy.Proxy(
                proxy.upstream_client(httpx.MockTransport(engine)), "http://pools.example.com:8080", {"coursework": "s"}
            ),
            pools=Pools(
                views=lambda: {"coursework": PoolView(ready=view.ready, note=view.note, session=booked)},
                clock=lambda: float(NOW),
            ),
            retry_after=retry_after or {},
        )
        self.app = build_app(forwarding_handlers(forwarding))

    def post(self, entry: routes.Route, *, stream: bool = False) -> httpx.Response:
        body = BODIES[entry] | ({"stream": True} if stream else {})

        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example.com") as client:
                return await client.post(entry.path, json=body, headers={"x-api-key": self.key})

        return asyncio.run(asyncio.wait_for(send(), timeout=10))


def assert_refused(gateway: Gateway, answer: httpx.Response, entry: routes.Route, cause: Cause, seconds: int) -> None:
    """The answer is the cause's 503, whole, in the route's wire format, and nothing was taken or sent for it."""
    assert answer.status_code == 503
    assert answer.headers["retry-after"] == str(seconds)
    # A JSON answer, for a stream too: nothing was answered 200, and nothing marks the request as held.
    assert answer.headers["content-type"] == "application/json"
    assert "x-rhi-held" not in answer.headers
    body = json.loads(answer.content)
    if entry.wire_format is routes.WireFormat.MESSAGES:
        assert body == {"type": "error", "error": {"type": cause.value, "message": cause.sentence}}
    else:
        assert body == {"error": {"message": cause.sentence, "type": cause.value, "param": None, "code": cause.value}}
    assert gateway.ledger.reserved == []
    assert gateway.reached == []


@pytest.mark.parametrize(("kept", "cause"), [STARTING, *NONE_STARTING])
@STREAMS
@FORMATS
def test_a_request_for_a_pool_with_no_healthy_target_is_answered_at_once_with_no_slot_taken(
    entry: routes.Route, stream: bool, kept: ScalerNote | None, cause: Cause
) -> None:
    gateway = Gateway(PoolView(note=kept))
    # The design's seconds: 30 for a pool that is starting, 60 for one that waits for capacity.
    seconds = 30 if cause is Cause.WARMING else 60
    assert_refused(gateway, gateway.post(entry, stream=stream), entry, cause, seconds)


@pytest.mark.parametrize(("kept", "cause"), [STARTING, *NONE_STARTING])
def test_a_token_count_gets_the_answer_a_request_would_get(kept: ScalerNote | None, cause: Cause) -> None:
    gateway = Gateway(PoolView(note=kept))
    counted = gateway.post(routes.COUNT_TOKENS)
    assert_refused(gateway, counted, routes.COUNT_TOKENS, cause, 30 if cause is Cause.WARMING else 60)
    asked = gateway.post(routes.MESSAGES)
    assert (counted.status_code, counted.content, counted.headers["retry-after"]) == (
        asked.status_code,
        asked.content,
        asked.headers["retry-after"],
    )


@pytest.mark.parametrize(
    ("kept", "cause", "seconds"), [(note(1), Cause.WARMING, 45), (note(0), Cause.CAPACITY_UNAVAILABLE, 90)]
)
@STREAMS
@FORMATS
def test_retry_after_is_the_configured_one(
    entry: routes.Route, stream: bool, kept: ScalerNote, cause: Cause, seconds: int
) -> None:
    gateway = Gateway(PoolView(note=kept), retry_after={Cause.WARMING: 45, Cause.CAPACITY_UNAVAILABLE: 90})
    assert_refused(gateway, gateway.post(entry, stream=stream), entry, cause, seconds)
    assert_refused(gateway, gateway.post(routes.COUNT_TOKENS), routes.COUNT_TOKENS, cause, seconds)


@pytest.mark.parametrize("entry", list(BODIES), ids=lambda entry: entry.path)
def test_a_pool_with_a_healthy_target_is_served_whatever_its_note_counts(entry: routes.Route) -> None:
    # The pool has an instance in service and another starting: the request is forwarded, with its slot.
    gateway = Gateway(PoolView(ready=True, note=note(1)))
    assert gateway.post(entry).status_code == 200
    assert (len(gateway.ledger.reserved), len(gateway.reached)) == (1, 1)
