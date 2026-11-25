"""A pool is served while its session is active (`rhi_gateway.readiness`, `rhi_gateway.app`, `rhi_gateway.local`).

A request for a pool whose session is not active ends with `no_session`, for every
key, and the status lists that pool unavailable with it; while no pool's session is active, a
request is refused before its body is read (system design, sections 5.3, 5.6 and 6.2, step 5).

The gateway runs whole over a real readiness reader, whose load-balancing API and pool-state table
are one double: the pools of `healthy` have a healthy target, and each pool's session is the item
the test writes. The proxy and the ledger record what reaches them, and the application
is reached through a `receive` that counts its calls: a body is read through nothing else. The
clock is the test's. Every key is generated when the test runs.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any

import httpx
import pytest
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from rhi_common import sessions
from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.records import Session
from rhi_common.students import CANARY_GITHUB_ID, StudentKey
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, Pools, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger, Reservation
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.readiness import Readiness

NOW = 1_800_000_000
HOUR = 3600
STUDENT = 4242
KEYS = pytest.mark.parametrize("github_id", [STUDENT, CANARY_GITHUB_ID], ids=["student", "canary"])
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
POOLS = parse_pools({"coursework": MOCK, "mock": MOCK})
FORWARDED = [routes.MESSAGES, routes.COUNT_TOKENS, routes.CHAT_COMPLETIONS, routes.RESPONSES]
ROUTES = pytest.mark.parametrize("entry", FORWARDED, ids=lambda entry: entry.path)


def body(entry: routes.Route, pool: str) -> dict[str, Any]:
    """A request the route's filter accepts, for the pool."""
    if entry == routes.RESPONSES:
        return {"model": pool, "input": "hello"}
    return {"model": pool, "messages": []} | ({"max_tokens": 100} if entry == routes.MESSAGES else {})


def session(pool: str, *, starts_at: int = NOW - 60, ends_at: int = NOW + HOUR, set_at: int = NOW - 60) -> Session:
    return Session(pool=pool, instances=1, starts_at=starts_at, ends_at=ends_at, set_by="owner", set_at=set_at)


class Aws:
    """`describe_target_health` and the pool-state table's `get_item`."""

    def __init__(self, healthy: set[str]) -> None:
        self.healthy = healthy
        self.sessions: dict[str, dict[str, Any]] = {}
        self.failing = False

    def describe_target_health(self, *, TargetGroupArn: str) -> dict[str, Any]:
        state = "healthy" if TargetGroupArn in self.healthy else "unhealthy"
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": state}}]}

    def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:
        if self.failing:
            raise ConnectionError("unreachable")
        if Key["record"] == "session" and Key["pool"] in self.sessions:
            return {"Item": self.sessions[Key["pool"]]}
        return {}


class Ledger(InMemoryLedger):
    def __init__(self) -> None:
        super().__init__(course_end=datetime.date(2027, 6, 30), clock=lambda: float(NOW))
        self.reserved: list[str] = []

    def reserve(self, student: Any, request_id: str, **rest: Any) -> Reservation:
        self.reserved.append(rest["pool"])
        return super().reserve(student, request_id, **rest)


class Reached(proxy.Proxy):
    """The proxy, which records each request that reaches it and answers 200 with no engine."""

    def __init__(self) -> None:
        super().__init__(
            proxy.upstream_client(httpx.MockTransport(lambda r: httpx.Response(500))),
            "http://pools.example.com:8080",
            {},
        )
        self.reached: list[tuple[routes.Route, str]] = []

    async def forward(self, route: routes.Route, forwarded: Any, on_end: Any, **rest: Any) -> Response:
        self.reached.append((route, forwarded.pool.name))
        await on_end(proxy.Observed(0, 0, ended_cleanly=True))
        return Response(status_code=200)


class Gateway:
    """The gateway with a key for a student and for the canary, after one readiness cycle.

    The table holds `sessions`, and the pools of `healthy` have a healthy target; with `failing`,
    that first cycle's reads fail. `now` is the gateway's clock, and `receives` counts the calls of
    the application's `receive`.
    """

    def __init__(
        self,
        *sessions: Session,
        healthy: tuple[str, ...] = ("coursework",),
        failing: bool = False,
        session_max_hours: int = 12,
    ) -> None:
        self.now = float(NOW)
        self.receives = 0
        self.aws = Aws({"tg-" + pool for pool in healthy})
        self.book(*sessions)
        self.aws.failing = failing
        clock = lambda: self.now
        self.readiness = Readiness({p.name: "tg-" + p.name for p in POOLS}, elbv2=self.aws, table=self.aws, clock=clock)
        self.readiness.refresh()
        self.keys = {github_id: generate_key() for github_id in (STUDENT, CANARY_GITHUB_ID)}
        authenticator = Authenticator(
            InMemoryKeyStore(*(self._item(github_id, key) for github_id, key in self.keys.items())),
            wall_clock=lambda: float(NOW),
        )
        self.ledger = Ledger()
        self.proxy = Reached()
        forwarding = Forwarding(
            authenticator=authenticator,
            ledger=self.ledger,
            filters={
                routes.MESSAGES: BodyFilter(messages.MESSAGES, POOLS),
                routes.COUNT_TOKENS: BodyFilter(messages.COUNT_TOKENS, POOLS),
                routes.CHAT_COMPLETIONS: ChatBodyFilter(POOLS),
                routes.RESPONSES: BodyFilter(responses.RESPONSES, POOLS),
            },
            proxy=self.proxy,
            pools=Pools(views=self.readiness.views, clock=clock, session_max_hours=session_max_hours),
        )
        local = LocalRoutes(
            authenticator,
            POOLS,
            self.readiness.views,
            default_pool=POOLS[0],
            clock=clock,
            session_max_hours=session_max_hours,
        )
        self.app = build_app(forwarding_handlers(forwarding), local=local)

    def book(self, *sessions: Session) -> None:
        """Write the sessions to the table: the gateway holds them after its next cycle."""
        self.aws.sessions.update({booked.pool: booked.to_item() for booked in sessions})

    @staticmethod
    def _item(github_id: int, key: str) -> StudentKey:
        return StudentKey(
            key_hash=key_hash(key),
            github_id=github_id,
            alias=f"student-{github_id}",
            installation=None,
            issued_at=NOW - 3600,
            expires_at=NOW + 90 * 86400,
            revoked_at=None,
            concurrency_limit=4,
        )

    async def _counting(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def counted() -> Message:
            self.receives += 1
            return await receive()

        await self.app(scope, counted, send)

    def call(self, method: str, path: str, github_id: int = STUDENT, content: object = None) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._counting)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway.example.com") as client:
                return await client.request(
                    method,
                    path,
                    headers={"x-api-key": self.keys[github_id]},
                    content=content if isinstance(content, bytes | None) else json.dumps(content),
                )

        return asyncio.run(send())

    def forward(self, entry: routes.Route, pool: str, github_id: int = STUDENT) -> httpx.Response:
        return self.call("POST", entry.path, github_id, body(entry, pool))

    def nothing_was_asked(self) -> bool:
        """No slot was taken and no engine reached."""
        return not (self.ledger.reserved or self.proxy.reached)


def no_session(answer: httpx.Response) -> bool:
    error = answer.json()["error"]
    return (
        answer.status_code == 503
        and "retry-after" not in answer.headers
        and Cause.NO_SESSION in (error["type"], error.get("code"))
    )


@ROUTES
@KEYS
def test_a_pool_is_served_in_its_session_and_a_pool_without_one_is_answered_no_session(
    entry: routes.Route, github_id: int
) -> None:
    # The pool with no session has no healthy target either: the answer is `no_session`, and no other cause.
    gateway = Gateway(session("coursework"))
    assert no_session(gateway.forward(entry, "mock", github_id))
    assert gateway.nothing_was_asked()

    assert gateway.forward(entry, "coursework", github_id).status_code == 200
    assert gateway.proxy.reached == [(entry, "coursework")]
    assert gateway.ledger.reserved == ["coursework"]


@ROUTES
@KEYS
def test_with_no_session_at_all_a_request_is_refused_before_its_body_is_read(
    entry: routes.Route, github_id: int
) -> None:
    gateway = Gateway(healthy=("coursework", "mock"))
    assert no_session(gateway.forward(entry, "coursework", github_id))
    assert gateway.receives == 0
    assert gateway.nothing_was_asked()

    # With a session on one pool the body is read: it names the pool, or is refused for what it holds.
    gateway.book(session("mock"))
    gateway.readiness.refresh()
    assert no_session(gateway.forward(entry, "coursework", github_id))
    assert gateway.receives > 0
    refused = gateway.call("POST", entry.path, github_id, b"{")
    assert refused.status_code == 400
    assert gateway.nothing_was_asked()


@ROUTES
def test_a_request_for_a_pool_without_a_session_is_answered_no_session_whatever_else_its_body_holds(
    entry: routes.Route,
) -> None:
    # An object where the route's allow-list describes an array: the filter refuses such a body.
    gateway = Gateway(session("coursework"))
    field = "input" if entry == routes.RESPONSES else "messages"

    def refused_body(pool: str) -> dict[str, Any]:
        return body(entry, pool) | {field: {"role": "user", "content": "hello"}}

    assert no_session(gateway.call("POST", entry.path, content=refused_body("mock")))
    # The same body for the pool in session is judged on its fields.
    assert gateway.call("POST", entry.path, content=refused_body("coursework")).status_code == 400
    assert gateway.nothing_was_asked()


@pytest.mark.parametrize(
    ("booked", "session_max_hours", "served"),
    [
        (session("coursework", starts_at=NOW, ends_at=NOW + 1, set_at=NOW - 5), 12, True),
        (session("coursework", starts_at=NOW + 1, set_at=NOW - 5), 12, False),
        (session("coursework", ends_at=NOW), 12, False),
        # Written to last 3 hours, 2 hours ago: over when the longest session is 1 hour, or 2.
        (session("coursework", starts_at=NOW - 2 * HOUR, ends_at=NOW + HOUR, set_at=NOW - 2 * HOUR), 3, True),
        (session("coursework", starts_at=NOW - 2 * HOUR, ends_at=NOW + HOUR, set_at=NOW - 2 * HOUR), 2, False),
    ],
    ids=["at-its-start", "before-its-start", "at-its-end", "within-the-longest-session", "past-the-longest-session"],
)
def test_a_session_is_active_from_its_start_to_its_end_under_the_longest_session(
    booked: Session, session_max_hours: int, served: bool
) -> None:
    gateway = Gateway(booked, session("mock"), healthy=("coursework", "mock"), session_max_hours=session_max_hours)
    answer = gateway.forward(routes.MESSAGES, "coursework")
    assert (answer.status_code == 200) if served else no_session(answer)
    states = {record["model"]: record for record in gateway.call("GET", "/status").json()["pools"]}
    assert (states["coursework"].get("cause") == "no_session") is not served
    # The status gives the end of a session that is active, as every reader takes it, and of no other.
    ends_at = sessions.end(booked, session_max_hours=session_max_hours) if served else None
    assert states["coursework"].get("session_ends_at") == ends_at
    assert states["mock"] == {"model": "mock", "state": "ready", "session_ends_at": NOW + HOUR}


def test_a_session_ends_on_the_gateways_own_clock_with_no_new_read() -> None:
    gateway = Gateway(session("coursework", ends_at=NOW + 600))
    gateway.now = NOW + 599.5
    assert gateway.forward(routes.MESSAGES, "coursework").status_code == 200
    gateway.now, read = NOW + 600.0, gateway.receives
    answer = gateway.forward(routes.MESSAGES, "coursework")
    # The only session there was has ended: the request is refused with its body unread.
    assert no_session(answer) and gateway.receives == read
    assert gateway.ledger.reserved == ["coursework"]


def test_a_pool_has_no_session_until_one_is_read_and_a_failed_read_keeps_the_session_last_read() -> None:
    # The session is in the table, but the gateway's first read of it fails.
    gateway = Gateway(session("coursework"), failing=True)
    assert no_session(gateway.forward(routes.MESSAGES, "coursework"))
    gateway.aws.failing = False
    gateway.readiness.refresh()
    assert gateway.forward(routes.MESSAGES, "coursework").status_code == 200

    # Stopped in the table, behind a read that fails: the session last read is kept, and still serves.
    gateway.book(session("coursework", ends_at=NOW - 1, set_at=NOW - 1))
    gateway.aws.failing = True
    gateway.readiness.refresh()
    assert gateway.forward(routes.MESSAGES, "coursework").status_code == 200
    gateway.aws.failing = False
    gateway.readiness.refresh()
    assert no_session(gateway.forward(routes.MESSAGES, "coursework"))
    assert gateway.proxy.reached == [(routes.MESSAGES, "coursework")] * 2


@KEYS
def test_the_status_lists_a_pool_unavailable_with_no_session_exactly_when_its_own_session_is_not_active(
    github_id: int,
) -> None:
    gateway = Gateway(session("coursework"), healthy=("coursework", "mock"))
    answer = gateway.call("GET", "/status", github_id)
    assert answer.status_code == 200
    assert answer.json()["pools"] == [
        {"model": "coursework", "state": "ready", "session_ends_at": NOW + HOUR},
        {"model": "mock", "state": "unavailable", "cause": "no_session"},
    ]
    # The models and the hello answer as ever.
    assert [m["id"] for m in gateway.call("GET", "/v1/models", github_id).json()["data"]] == ["coursework", "mock"]
    assert gateway.call("HEAD", "/api/hello").status_code == 200
