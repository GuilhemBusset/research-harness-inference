"""The session switch at the gateway (`rhi_gateway.readiness`, `rhi_gateway.app`, `rhi_gateway.local`).

While no session is on, a student's request and wake end at once with `no_session`, and a student's
status lists every pool unavailable with it; the owner's own entries, the canary and the spikes'
students, are served whatever the switch says (system design, section 5.6).

The gateway runs whole over a real readiness reader, whose load-balancing API and pool-state table
are one double: every pool has a healthy target, and the switch is the item the test writes. The
proxy, the ledger and the waker record what reaches them. Every key is generated when the test runs.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any

import httpx
import pytest
from starlette.responses import Response

from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.records import SessionSwitch
from rhi_common.students import CANARY_GITHUB_ID, SPIKE_GITHUB_ID_BASE, StudentKey
from rhi_gateway import proxy, routes
from rhi_gateway.app import Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger, Reservation
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.readiness import Readiness
from rhi_gateway.waker import Waker

NOW = 1_800_000_000
STUDENT = 4242
SPIKE_STUDENT = SPIKE_GITHUB_ID_BASE + 1
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
BODIES: dict[routes.Route, dict[str, Any]] = {
    routes.MESSAGES: {"model": "coursework", "max_tokens": 100, "messages": []},
    routes.COUNT_TOKENS: {"model": "coursework", "messages": []},
    routes.CHAT_COMPLETIONS: {"model": "coursework", "messages": []},
    routes.RESPONSES: {"model": "coursework", "input": "hello"},
}
FORWARDED = list(BODIES)


class Aws:
    """`describe_target_health` (every target healthy), the pool-state table's `get_item`, the scaler's `invoke`."""

    def __init__(self) -> None:
        self.switch: dict[str, Any] | None = None
        self.failing = False
        self.invocations: list[dict[str, Any]] = []

    def describe_target_health(self, *, TargetGroupArn: str) -> dict[str, Any]:
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy"}}]}

    def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:
        if self.failing:
            raise ConnectionError("unreachable")
        if Key["pool"] == "_session" and self.switch is not None:
            return {"Item": self.switch}
        return {}

    def set_switch(self, on: bool) -> None:
        self.switch = SessionSwitch(on=on, set_by="owner", set_at=NOW).to_item()

    def invoke(self, **arguments: Any) -> dict[str, Any]:
        self.invocations.append(arguments)
        return {"StatusCode": 202}


class Ledger(InMemoryLedger):
    def __init__(self) -> None:
        super().__init__(course_end=datetime.date(2027, 6, 30), clock=lambda: float(NOW))
        self.reserved: list[str] = []

    def reserve(self, student: Any, request_id: str, **rest: Any) -> Reservation:
        self.reserved.append(request_id)
        return super().reserve(student, request_id, **rest)


class Reached(proxy.Proxy):
    """The proxy, which records each request that reaches it and answers 200 with no engine."""

    def __init__(self) -> None:
        super().__init__(
            proxy.upstream_client(httpx.MockTransport(lambda r: httpx.Response(500))),
            "http://pools.example.com:8080",
            {},
        )
        self.reached: list[routes.Route] = []

    async def forward(self, route: routes.Route, forwarded: Any, on_end: Any, **rest: Any) -> Response:
        self.reached.append(route)
        await on_end(proxy.Observed(0, 0, ended_cleanly=True))
        return Response(status_code=200)


class RecordingWaker(Waker):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.recorded: list[str] = []
        self.asked: list[str] = []

    def record(self, pool: str) -> None:
        self.recorded.append(pool)
        super().record(pool)

    def ask(self, pool: str) -> Any:
        self.asked.append(pool)
        return super().ask(pool)


class Gateway:
    """The gateway with a key for a student, the canary and a spike's student, after one readiness cycle.

    The table holds the switch as `session`; with `failing`, that first cycle's reads fail.
    """

    def __init__(self, *, session: bool, failing: bool = False) -> None:
        self.aws = Aws()
        self.aws.set_switch(session)
        self.aws.failing = failing
        self.readiness = Readiness(
            {p.name: "tg-" + p.name for p in POOLS}, elbv2=self.aws, table=self.aws, clock=lambda: NOW
        )
        self.readiness.refresh()
        self.keys = {github_id: generate_key() for github_id in (STUDENT, CANARY_GITHUB_ID, SPIKE_STUDENT)}
        authenticator = Authenticator(
            InMemoryKeyStore(*(self._item(github_id, key) for github_id, key in self.keys.items())),
            wall_clock=lambda: float(NOW),
        )
        self.ledger = Ledger()
        self.proxy = Reached()
        self.hold_wakes: list[str] = []
        self.waker = RecordingWaker(self.readiness, lambda_client=self.aws, function_name="scaler", clock=lambda: NOW)
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
            holding=Holding(
                views=self.readiness.views, wake=self.hold_wakes.append, session=self.readiness.session_is_on
            ),
        )
        local = LocalRoutes(
            authenticator,
            POOLS,
            self.readiness.views,
            default_pool=POOLS[0],
            clock=lambda: NOW,
            waker=self.waker,
            session=self.readiness.session_is_on,
        )
        self.app = build_app(forwarding_handlers(forwarding), local=local)

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

    def call(self, method: str, path: str, github_id: int = STUDENT, body: object = None) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway.example.com") as client:
                return await client.request(
                    method,
                    path,
                    headers={"x-api-key": self.keys[github_id]},
                    content=None if body is None else json.dumps(body),
                )

        return asyncio.run(send())

    def forward(self, entry: routes.Route, github_id: int = STUDENT) -> httpx.Response:
        return self.call("POST", entry.path, github_id, BODIES[entry])


def no_session(answer: httpx.Response) -> bool:
    error = answer.json()["error"]
    return (
        answer.status_code == 503
        and "retry-after" not in answer.headers
        and Cause.NO_SESSION in (error["type"], error.get("code"))
    )


@pytest.mark.parametrize("entry", FORWARDED, ids=lambda entry: entry.path)
def test_a_students_request_ends_with_no_session_while_the_switch_is_off(entry: routes.Route) -> None:
    gateway = Gateway(session=False)
    assert no_session(gateway.forward(entry))
    assert gateway.ledger.reserved == [] and gateway.proxy.reached == []
    assert gateway.hold_wakes == [] and gateway.waker.asked == [] and gateway.aws.invocations == []


def test_a_students_wake_ends_with_no_session_and_asks_for_nothing() -> None:
    gateway = Gateway(session=False)
    assert no_session(gateway.call("POST", "/wake", body={"model": "coursework"}))
    assert gateway.waker.recorded == [] and gateway.waker.asked == [] and gateway.aws.invocations == []


def test_a_students_status_lists_every_pool_unavailable_with_no_session() -> None:
    gateway = Gateway(session=False)
    answer = gateway.call("GET", "/status")
    assert answer.status_code == 200
    assert answer.json()["pools"] == [{"model": p.name, "state": "unavailable", "cause": "no_session"} for p in POOLS]
    # The canary sees the pools as they are; and the models and the hello answer as ever.
    assert {p["state"] for p in gateway.call("GET", "/status", CANARY_GITHUB_ID).json()["pools"]} == {"ready"}
    assert [m["id"] for m in gateway.call("GET", "/v1/models").json()["data"]] == [p.name for p in POOLS]
    assert gateway.call("HEAD", "/api/hello").status_code == 200


@pytest.mark.parametrize("github_id", [CANARY_GITHUB_ID, SPIKE_STUDENT], ids=["canary", "spike"])
def test_the_owners_entries_are_served_while_the_switch_is_off(github_id: int) -> None:
    gateway = Gateway(session=False)
    for entry in FORWARDED:
        assert gateway.forward(entry, github_id).status_code == 200
    assert gateway.proxy.reached == FORWARDED
    answer = gateway.call("POST", "/wake", github_id, {"model": "coursework"})
    assert answer.status_code == 200 and answer.json()["state"] == "ready"


def test_a_students_request_is_forwarded_while_the_switch_is_on() -> None:
    gateway = Gateway(session=True)
    for entry in FORWARDED:
        assert gateway.forward(entry).status_code == 200
    assert gateway.proxy.reached == FORWARDED
    assert len(gateway.ledger.reserved) == len(FORWARDED)
    assert {p["state"] for p in gateway.call("GET", "/status").json()["pools"]} == {"ready"}


def test_the_switch_reads_off_until_read() -> None:
    # The switch is on in the table, but the gateway's first read of it fails: it reads off.
    gateway = Gateway(session=True, failing=True)
    assert not gateway.readiness.session_is_on()
    assert no_session(gateway.forward(routes.MESSAGES))

    # A gateway that read it on, then fails to read it turned off, keeps the value it last read.
    gateway = Gateway(session=True)
    gateway.aws.set_switch(False)
    gateway.aws.failing = True
    gateway.readiness.refresh()
    assert gateway.forward(routes.MESSAGES).status_code == 200
    assert gateway.proxy.reached == [routes.MESSAGES]
    # Once a read succeeds again, the switch is what it says.
    gateway.aws.failing = False
    gateway.readiness.refresh()
    assert no_session(gateway.forward(routes.MESSAGES))
