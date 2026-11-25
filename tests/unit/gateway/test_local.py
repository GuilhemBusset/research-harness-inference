"""The routes the gateway answers itself (`rhi_gateway.local`), through the application.

Every key is generated when the tests run. The ledgers' methods are replaced by recorders for
every test, so that any reservation a route made would show.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from starlette.types import Message

from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import Pool, parse_pools
from rhi_common.records import ScalerNote, Session
from rhi_common.students import StudentKey
from rhi_gateway import budget, routes
from rhi_gateway.app import build_app
from rhi_gateway.auth import Authenticator
from rhi_gateway.keystore import InMemoryKeyStore, KeyRecord
from rhi_gateway.local import LocalRoutes, State
from rhi_gateway.readiness import PoolView

NOW = 1_800_000_000


def pools(*names: str) -> tuple[Pool, ...]:
    entry = {
        "engine": "mock",
        "instance_types": ["t4g.small"],
        "prices": {"t4g.small": 0.0168},
        "max_instances": 1,
        "warm_instances": 0,
        "wake": "on-demand",
        "target_concurrency": 64,
        "context_length": 8192,
        "largest_output": 1024,
    }
    return parse_pools(dict.fromkeys(names, entry))


def note(starting: int) -> ScalerNote:
    """The scaler's note of a pool it asked one instance for, with this many starting."""
    return ScalerNote(pool="p", capacity_asked=1, no_capacity_at=None, written_at=NOW, starting=starting)


@dataclass
class CountingStore:
    """The in-memory store, counting its lookups."""

    inner: InMemoryKeyStore
    lookups: int = 0

    def lookup(self, key_hash: str) -> KeyRecord | None:
        self.lookups += 1
        return self.inner.lookup(key_hash)


@dataclass
class Gateway:
    """The application with the local routes over two pools, `coursework` the default; the test sets the views and the time.

    Each pool is in a session, active for an hour from `NOW`, whatever view the test sets for it,
    but a pool of `unbooked`, which has none.
    """

    store: CountingStore
    key: str = field(default_factory=generate_key)
    views: dict[str, PoolView] = field(default_factory=dict)
    now: float = NOW
    unbooked: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        configured = pools("coursework", "mock")
        local = LocalRoutes(
            Authenticator(self.store, wall_clock=lambda: NOW),
            configured,
            self.in_session,
            default_pool=configured[0],
            clock=lambda: self.now,
        )
        self.app = build_app(local=local)

    def in_session(self) -> dict[str, PoolView]:
        booked = {
            name: Session(name, instances=1, starts_at=NOW, ends_at=NOW + 3600, set_by="owner", set_at=NOW)
            for name in ("coursework", "mock")
            if name not in self.unbooked
        }
        return {
            name: replace(self.views.get(name, PoolView()), session=booked.get(name)) for name in ("coursework", "mock")
        }

    def send(self, method: str, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
        """The status and the body the application answers."""
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "http",
            "server": ("gateway.example.com", 80),
            "client": ("192.0.2.1", 50000),
            "method": method,
            "root_path": "",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
            "state": {},
        }
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def keep(message: Message) -> None:
            sent.append(message)

        asyncio.run(self.app(scope, receive, keep))
        return sent[0]["status"], b"".join(message.get("body", b"") for message in sent[1:])

    def get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        status, body = self.send("GET", path, headers if headers is not None else {"x-api-key": self.key})
        return status, json.loads(body)


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[Gateway]:
    """The gateway; any reservation made or settled, by any ledger, fails the test."""
    made: list[str] = []
    targets = [(cls, name) for cls in (budget.InMemoryLedger, budget.DynamoDBLedger) for name in ("reserve", "settle")]
    for owner, name in targets:
        monkeypatch.setattr(owner, name, lambda *_, _name=name, **__: made.append(_name))
    key = generate_key()
    student = StudentKey(
        key_hash=key_hash(key),
        github_id=4242,
        alias="otter",
        installation=None,
        issued_at=NOW - 3600,
        expires_at=NOW + 86400,
        revoked_at=None,
        concurrency_limit=4,
    )
    yield Gateway(CountingStore(InMemoryKeyStore(student)), key)
    assert made == []


def test_models_lists_every_pool_and_the_default(gateway: Gateway) -> None:
    status, body = gateway.get("/v1/models", {"Authorization": "Bearer " + gateway.key})
    assert status == 200
    assert [model["id"] for model in body["data"]] == ["coursework", "mock"]
    assert [model["default"] for model in body["data"]] == [True, False]
    assert body["default"] == "coursework"


def test_status_lists_every_pool_with_its_state(gateway: Gateway) -> None:
    gateway.views["coursework"] = PoolView(ready=True)
    gateway.unbooked = {"mock"}
    assert gateway.get("/status") == (
        200,
        {
            "pools": [
                {"model": "coursework", "state": "ready", "session_ends_at": NOW + 3600},
                {"model": "mock", "state": "unavailable", "cause": "no_session"},
            ]
        },
    )


@pytest.mark.parametrize("path", ["/v1/models", "/status"])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer junk"}])
def test_refused_without_a_valid_key_and_no_lookup(gateway: Gateway, path: str, headers: dict[str, str]) -> None:
    status, body = gateway.get(path, headers)
    assert status == 401
    assert body["error"]["type"] == Cause.INVALID_KEY.value
    assert gateway.store.lookups == 0


def test_hello_answers_with_no_key_and_no_body(gateway: Gateway) -> None:
    assert gateway.send("HEAD", "/api/hello", {}) == (200, b"")
    assert gateway.store.lookups == 0


def test_a_handler_for_a_local_route_is_refused_beside_the_local_routes() -> None:
    local = LocalRoutes(Authenticator(InMemoryKeyStore()), pools("mock"), dict)
    with pytest.raises(ValueError, match="local routes"):
        build_app({routes.HELLO: local.handlers()[routes.HELLO]}, local=local)


# The status table of the design (section 5.3): what the gateway keeps of the pool, whether its
# session is active, then the state and the cause.
STATUS_TABLE = [
    pytest.param(PoolView(ready=True), True, "ready", None, id="ready"),
    pytest.param(PoolView(note=note(starting=1)), True, "warming", None, id="warming"),
    pytest.param(PoolView(note=note(starting=0)), True, "unavailable", "capacity_unavailable", id="none starting"),
    pytest.param(PoolView(), True, "unavailable", "capacity_unavailable", id="no note"),
    pytest.param(PoolView(), False, "unavailable", "no_session", id="no session"),
    # A pool whose session is not active is `no_session`, whatever else the gateway keeps of it.
    pytest.param(
        PoolView(ready=True, note=note(starting=1)), False, "unavailable", "no_session", id="no session, healthy"
    ),
]


@pytest.mark.parametrize(("view", "booked", "state", "cause"), STATUS_TABLE)
def test_the_status_table(gateway: Gateway, view: PoolView, booked: bool, state: str, cause: str | None) -> None:
    gateway.views["mock"] = view
    gateway.unbooked = set() if booked else {"mock"}
    status, body = gateway.get("/status")

    expected: dict[str, Any] = {"model": "mock", "state": state}
    if cause is not None:
        expected["cause"] = cause
    if booked:
        # The session is active: the record gives the time it ends.
        expected["session_ends_at"] = NOW + 3600
    assert (status, body["pools"][1]) == (200, expected)
    # No pool is ever `cold`: the status has three states.
    assert {member.value for member in State} == {"ready", "warming", "unavailable"}


def test_a_session_that_has_ended_is_no_session(gateway: Gateway) -> None:
    gateway.views["mock"] = PoolView(ready=True)
    gateway.now = NOW + 3600
    _, body = gateway.get("/status")
    assert body["pools"][1] == {"model": "mock", "state": "unavailable", "cause": "no_session"}
