"""The routes the gateway answers itself (`rhi_gateway.local`), through the application.

Every key is generated when the tests run. The waker's and the ledgers' methods are replaced by
recorders for every test, so that any wake or reservation a route made would show.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from starlette.types import Message

from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import Pool, parse_pools
from rhi_common.records import Mode, PoolState, ScalerNote
from rhi_common.students import StudentKey
from rhi_gateway import budget, routes, waker
from rhi_gateway.app import build_app
from rhi_gateway.auth import Authenticator
from rhi_gateway.errors import WireFormat
from rhi_gateway.hold import End, Hold
from rhi_gateway.keystore import InMemoryKeyStore, KeyRecord
from rhi_gateway.local import LocalRoutes, State, pool_status
from rhi_gateway.readiness import PoolView

NOW = 1_800_000_000


def pools(*names: str, wake: str = "on-demand") -> tuple[Pool, ...]:
    entry = {
        "engine": "mock",
        "instance_types": ["t4g.small"],
        "prices": {"t4g.small": 0.0168},
        "max_instances": 1,
        "warm_instances": 0,
        "wake": wake,
        "target_concurrency": 64,
        "context_length": 8192,
        "largest_output": 1024,
    }
    return parse_pools(dict.fromkeys(names, entry))


def note(asked: int = 0, no_capacity_at: int | None = None) -> ScalerNote:
    return ScalerNote(pool="p", capacity_asked=asked, no_capacity_at=no_capacity_at, written_at=NOW)


def mode(value: Mode) -> PoolState:
    return PoolState(pool="p", mode=value, set_by="admin", set_at=NOW)


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
    """The application with the local routes over two pools, `coursework` the default; the test sets the views and the time."""

    store: CountingStore
    key: str = field(default_factory=generate_key)
    views: dict[str, PoolView] = field(default_factory=dict)
    now: float = NOW

    def __post_init__(self) -> None:
        configured = pools("coursework", "mock")
        local = LocalRoutes(
            Authenticator(self.store, wall_clock=lambda: NOW),
            configured,
            lambda: self.views,
            default_pool=configured[0],
            clock=lambda: self.now,
        )
        self.app = build_app(local=local)

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
    """The gateway; any wake asked for or reservation made or settled, by any waker or ledger, fails the test."""
    made: list[str] = []
    targets: list[tuple[type, str]] = [(waker.Waker, "ask"), (waker.Waker, "record")]
    targets += [(cls, name) for cls in (budget.InMemoryLedger, budget.DynamoDBLedger) for name in ("reserve", "settle")]
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
        daily_token_budget=2_000_000,
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
    assert gateway.get("/status") == (
        200,
        {"pools": [{"model": "coursework", "state": "ready"}, {"model": "mock", "state": "cold"}]},
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


def test_the_four_states() -> None:
    (pool,) = pools("p")
    (scheduled,) = pools("p", wake="scheduled-only")
    cases = [
        (pool, PoolView(ready=True), State.READY, None),
        (pool, PoolView(note=note(asked=1)), State.WARMING, None),
        (pool, PoolView(note=note(asked=0)), State.COLD, None),
        (pool, PoolView(), State.COLD, None),
        (pool, PoolView(ready=True, mode=mode(Mode.DISABLED)), State.UNAVAILABLE, Cause.POOL_DISABLED),
        (scheduled, PoolView(), State.UNAVAILABLE, Cause.POOL_SCHEDULED_ONLY),
        (pool, PoolView(mode=mode(Mode.SCHEDULED_ONLY), note=note()), State.UNAVAILABLE, Cause.POOL_SCHEDULED_ONLY),
        (scheduled, PoolView(note=note(asked=1)), State.WARMING, None),
    ]
    for configured, view, state, cause in cases:
        status = pool_status(configured, view, NOW)
        assert (status.state, status.cause) == (state, cause), view
        if cause is not None:
            # The cause is the one the hold's rule ends a request with at once.
            held = Hold(configured, WireFormat.MESSAGES, streams=True, started_at=NOW)
            the_mode = view.mode.mode if view.mode else Mode.SCHEDULED_ONLY
            assert held.step(NOW, mode=the_mode, note=view.note, ready=view.ready) == (End(cause),)


def test_no_capacity_reads_unavailable_for_10_minutes(gateway: Gateway) -> None:
    for asked, after in ((0, "cold"), (1, "warming")):
        gateway.now = NOW
        gateway.views["mock"] = PoolView(note=note(asked=asked, no_capacity_at=NOW - 9 * 60))
        _, body = gateway.get("/status")
        assert body["pools"][1] == {"model": "mock", "state": "unavailable", "cause": "capacity_unavailable"}

        gateway.now = NOW + 2 * 60
        _, body = gateway.get("/status")
        assert body["pools"][1] == {"model": "mock", "state": after}
