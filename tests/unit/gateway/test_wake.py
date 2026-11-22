"""The wake (`POST /wake`, `rhi_gateway.local`), through the application.

The waker is the real one, over a real readiness whose AWS clients are doubles. The wake is given
no ledger: it takes no concurrency slot and refuses no student for a budget. Every key is generated
when the tests run.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

import pytest
from starlette.types import Message

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import Pool, parse_pools
from rhi_common.records import Mode, PoolState, ScalerNote
from rhi_common.students import StudentKey
from rhi_gateway.app import build_app
from rhi_gateway.auth import Authenticator
from rhi_gateway.budget import DynamoDBLedger, InMemoryLedger
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.readiness import Readiness
from rhi_gateway.waker import Waker

NOW = 1_800_000_000
GITHUB_ID = 4242


def pool(name: str, wake: str = "on-demand") -> Pool:
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
    return parse_pools({name: entry})[0]


POOLS = (pool("coursework"), pool("mock"), pool("evening", wake="scheduled-only"))


class Doubles:
    """`describe_target_health` (no healthy target), `get_item` on the pool-state table, and the scaler's `invoke`."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.invocations: list[dict[str, Any]] = []

    def put(self, record: PoolState | ScalerNote) -> None:
        item = record.to_item()
        self.items[str(item["pool"]), str(item["record"])] = item

    def describe_target_health(self, *, TargetGroupArn: str) -> dict[str, Any]:
        return {"TargetHealthDescriptions": []}

    def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:
        item = self.items.get((Key["pool"], Key["record"]))
        return {} if item is None else {"Item": item}

    def invoke(self, **arguments: Any) -> dict[str, Any]:
        self.invocations.append(arguments)
        return {"StatusCode": 202}


class KeptWaker(Waker):
    """The waker, keeping what each ask returned so that a test can wait for the invocation."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.asks: list[Future[None]] = []

    def ask(self, pool: str) -> Future[None]:
        self.asks.append(super().ask(pool))
        return self.asks[-1]


@dataclass
class Gateway:
    """The application with the local routes and the wake's parts; the test writes the pools' records."""

    budget: int = 2_000_000
    key: str = field(default_factory=generate_key)
    aws: Doubles = field(default_factory=Doubles)

    def __post_init__(self) -> None:
        self.student = StudentKey(
            key_hash=key_hash(self.key),
            github_id=GITHUB_ID,
            alias="otter",
            installation=None,
            issued_at=NOW - 3600,
            expires_at=NOW + 86400,
            revoked_at=None,
            daily_token_budget=self.budget,
            concurrency_limit=4,
        )
        self.readiness = Readiness(
            {p.name: "tg-" + p.name for p in POOLS}, elbv2=self.aws, table=self.aws, clock=lambda: NOW
        )
        self.waker = KeptWaker(self.readiness, lambda_client=self.aws, function_name="scaler", clock=lambda: NOW)
        self.asks = self.waker.asks
        local = LocalRoutes(
            Authenticator(InMemoryKeyStore(self.student), wall_clock=lambda: NOW),
            POOLS,
            self.readiness.views,
            default_pool=POOLS[0],
            clock=lambda: NOW,
            waker=self.waker,
        )
        self.app = build_app(local=local)

    def wake(self, model: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        """Read the pools' records once, then send the wake; the status and the JSON answered."""
        self.readiness.refresh()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "http",
            "server": ("gateway.example.com", 80),
            "client": ("192.0.2.1", 50000),
            "method": "POST",
            "root_path": "",
            "path": "/wake",
            "raw_path": b"/wake",
            "query_string": b"",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in (headers if headers is not None else {"x-api-key": self.key}).items()
            ],
            "state": {},
        }
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": json.dumps({"model": model}).encode(), "more_body": False}

        async def keep(message: Message) -> None:
            sent.append(message)

        asyncio.run(self.app(scope, receive, keep))
        return sent[0]["status"], json.loads(b"".join(message.get("body", b"") for message in sent[1:]))

    def scaler_calls(self) -> list[dict[str, Any]]:
        """The scaler's invocations, once every asked wake has run."""
        for asked in self.asks:
            asked.result(timeout=10)
        return self.aws.invocations

    def woken(self) -> tuple[int, int]:
        """How many wakes were asked of the scaler and how many explicit wakes are pending, over every pool."""
        return len(self.scaler_calls()), sum(self.waker.pending(p.name) for p in POOLS)


def mode(name: str, value: Mode) -> PoolState:
    return PoolState(pool=name, mode=value, set_by="admin", set_at=NOW)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer junk"}, {"x-api-key": generate_key()}])
def test_refused_without_a_valid_key(headers: dict[str, str]) -> None:
    gateway = Gateway()
    status, body = gateway.wake("mock", headers)
    assert (status, body["error"]["type"]) == (401, "invalid_key")
    assert gateway.woken() == (0, 0)


def test_a_cold_pool_is_woken_once_and_its_record_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[str] = []
    for ledger in (InMemoryLedger, DynamoDBLedger):
        for name in ("reserve", "settle", "withdraw"):
            monkeypatch.setattr(ledger, name, lambda *_, _name=name, **__: written.append(_name))
    gateway = Gateway()
    assert gateway.wake("mock") == (200, {"model": "mock", "state": "cold"})
    assert gateway.woken() == (1, 1)
    assert gateway.waker.pending("mock") == 1
    assert json.loads(gateway.scaler_calls()[0]["Payload"]) == {"pool": "mock"}
    # No concurrency slot is taken, by any ledger.
    assert written == []


def test_a_claude_name_wakes_the_default_pool_and_an_unknown_name_is_refused() -> None:
    gateway = Gateway()
    assert gateway.wake("claude-sonnet")[1] == {"model": "coursework", "state": "cold"}
    status, body = gateway.wake("no-such-pool")
    assert (status, body["error"]["type"]) == (400, "request_refused")
    assert gateway.waker.pending("coursework") == 1
    assert gateway.woken() == (1, 1)


@pytest.mark.parametrize(
    ("name", "record", "cause"),
    [
        ("mock", mode("mock", Mode.DISABLED), "pool_disabled"),
        ("evening", None, "pool_scheduled_only"),
    ],
)
def test_stopped_pools_are_not_woken(name: str, record: PoolState | None, cause: str) -> None:
    gateway = Gateway()
    if record is not None:
        gateway.aws.put(record)
    assert gateway.wake(name) == (200, {"model": name, "state": "unavailable", "cause": cause})
    assert gateway.woken() == (0, 0)


def test_a_key_with_a_daily_budget_of_0_still_wakes() -> None:
    gateway = Gateway(budget=0)
    assert gateway.wake("mock") == (200, {"model": "mock", "state": "cold"})
    assert gateway.woken() == (1, 1)


def test_no_capacity_still_wakes() -> None:
    gateway = Gateway()
    gateway.aws.put(ScalerNote(pool="mock", capacity_asked=1, no_capacity_at=NOW - 60, written_at=NOW))
    assert gateway.wake("mock") == (200, {"model": "mock", "state": "unavailable", "cause": "capacity_unavailable"})
    assert gateway.woken() == (1, 1)
