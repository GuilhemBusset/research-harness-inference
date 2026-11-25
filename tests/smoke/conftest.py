"""The stack the smoke tests run against, and the pool under test (system design, section 8.2, step 6).

The same tests verify a deployment from the pipeline and run on a laptop, in the implement loop
and in CI with no account. The environment says which:

| Variable | Holds | When unset |
| --- | --- | --- |
| `RHI_SMOKE_ENDPOINT` | The address of a deployed endpoint, a URL or a host name reached over HTTPS | The tests run against the local stack |
| `RHI_SMOKE_KEY` | The canary's key, when an endpoint is given | With an endpoint: the stack is refused, by the variable's name |
| `RHI_SMOKE_POOL` | The pool under test | The mock pool, `mock` |

**The local stack** is built in this process, and nothing of it leaves the machine:

- the gateway, as `rhi_gateway.main.build` builds it from an environment, served by uvicorn on a
  loopback port the system picks, so that the tests reach either stack the same way, by address;
- its tables and the pool's secret in moto, with the canary's key and the secret drawn when the
  tests run, and a session booked on the pool under test for the hour that starts when the stack
  is built: the gateway serves a pool only in its session, the canary's key like any other
  (section 5.6), so the stack is given to the tests once its gateway has read that session;
- the mock engine (`rhi_mock_engine.app`) as the transport of the gateway's hop to its pool;
- `Targets` for the target group's health, and `Scaler` for the scaler function: the pool has no
  target until the gateway asks for a wake, and its target is healthy `START_SECONDS` later. So
  the local stack's pool always starts cold.

The stack is one test file's: the fixture `stack` is built for a module and taken down after it,
so moto and its placeholder credentials never outlive the smoke tests in a run of every test.

**The deployed-only tests**, under `deployed/`, are left out of collection while no endpoint is
given, so that a local run skips nothing: `deployed/conftest.py` does that, and lists what more
they read of the environment. They run after the other smoke tests.

**The IDs.** Every smoke test carries its ID as the marker `smoke_id`; `ids.py` lists them.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest import mock

import boto3
import httpx
import pytest
import uvicorn
from botocore.client import BaseClient
from moto import mock_aws

from rhi_common import sessions, tables
from rhi_common.keys import generate_key, key_hash
from rhi_common.records import Session
from rhi_common.students import StudentKey
from rhi_gateway.main import aws_clients, build
from rhi_gateway.settings import Settings, read_settings
from rhi_gateway.sse import SseEvent, SseReader
from rhi_mock_engine.app import Settings as MockSettings
from rhi_mock_engine.app import create_app

ENDPOINT_VARIABLE = "RHI_SMOKE_ENDPOINT"
KEY_VARIABLE = "RHI_SMOKE_KEY"
POOL_VARIABLE = "RHI_SMOKE_POOL"
MOCK_POOL = "mock"

# How long after the first wake the local pool's target is healthy.
START_SECONDS = 1.0
# The interval of the local gateway's keep-alives: several fit in the time its pool takes to turn
# ready, which is the next of the gateway's readiness reads, 5 seconds apart.
KEEPALIVE_SECONDS = 1
# How long the client waits for the next bytes of an answer. A held stream is kept alive every 10
# seconds on a deployed stack, and the engine's own start is covered by the hold.
READ_SECONDS = 120.0

# Placeholders, which moto never checks.
_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_TABLES = {
    "RHI_KEYS_TABLE": (tables.KEYS, "smoke-keys"),
    "RHI_USAGE_TABLE": (tables.USAGE, "smoke-usage"),
    "RHI_HEARTBEAT_TABLE": (tables.HEARTBEATS, "smoke-heartbeats"),
    "RHI_POOL_STATE_TABLE": (tables.POOL_STATE, "smoke-pool-state"),
}
# The local pool: the mock engine, woken on demand.
_POOL = {
    "engine": "mock",
    "instance_types": ["t4g.small"],
    "prices": {"t4g.small": 0.02},
    "wake": "on-demand",
    "max_instances": 2,
    "warm_instances": 1,
    "target_concurrency": 8,
    "context_length": 262144,
    "largest_output": 32000,
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "smoke_id(id): the smoke test's fixed ID, one of tests/smoke/ids.py")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """The deployed-only tests run after the others, whatever their folder's name: they send the
    pool a stream, and the first stream of a run must be the one whose wake and hold are asserted."""
    deployed = Path(__file__).resolve().parent / "deployed"
    items.sort(key=lambda item: deployed in item.path.parents)


class TargetError(ValueError):
    """The environment does not say which stack to test. The message names a variable, never a value."""


@dataclass(frozen=True)
class Target:
    """What the environment asks for: an endpoint with the canary's key, or the local stack, and the pool."""

    # None: the local stack.
    endpoint: str | None
    key: str | None
    pool: str


def read_target(environ: Mapping[str, str]) -> Target:
    """The stack and the pool the environment names. An endpoint without a key is refused: the
    tests never fall back to the local stack when a deployment was to be verified."""
    pool = environ.get(POOL_VARIABLE) or MOCK_POOL
    endpoint = environ.get(ENDPOINT_VARIABLE)
    if not endpoint:
        return Target(endpoint=None, key=None, pool=pool)
    key = environ.get(KEY_VARIABLE)
    if not key:
        raise TargetError(f"{KEY_VARIABLE} is unset or empty: {ENDPOINT_VARIABLE} names an endpoint, which needs a key")
    address = endpoint if "://" in endpoint else f"https://{endpoint}"
    return Target(endpoint=address.rstrip("/"), key=key, pool=pool)


@dataclass(frozen=True)
class Stream:
    """A streamed answer, read to its end: its status and the events and comments it carried, in order."""

    status: int
    events: tuple[SseEvent, ...]


@dataclass(frozen=True)
class Stack:
    """The stack under test, and how a test reaches it. `deployed` is whether an endpoint was given."""

    endpoint: str
    key: str
    pool: str
    deployed: bool
    http: httpx.Client

    def request(self, method: str, path: str, *, keyed: bool = True, **arguments: Any) -> httpx.Response:
        """One request to the endpoint, with the canary's key unless `keyed` is false."""
        headers = {"Authorization": f"Bearer {self.key}"} if keyed else {}
        return self.http.request(method, self.endpoint + path, headers=headers, **arguments)

    def pool_state(self) -> str:
        """The state `GET /status` gives the pool under test now."""
        answer = self.request("GET", "/status")
        assert answer.status_code == 200, f"GET /status answered {answer.status_code}"
        states = {record["model"]: record["state"] for record in answer.json()["pools"]}
        assert self.pool in states, f"GET /status lists no pool {self.pool!r}"
        return states[self.pool]

    def stream(self, path: str, body: Mapping[str, Any]) -> Stream:
        """A streamed request with the canary's key, read to the end of its answer."""
        reader, events = SseReader(), []
        headers = {"Authorization": f"Bearer {self.key}"}
        with self.http.stream("POST", self.endpoint + path, headers=headers, json=body) as answer:
            for chunk in answer.iter_bytes():
                events += reader.feed(chunk)
        return Stream(status=answer.status_code, events=tuple(events))


class Targets:
    """`describe_target_health`, for the local stack: the pool's target group has no target until
    `start`, then one target, healthy once its start has taken `START_SECONDS`."""

    def __init__(self) -> None:
        self._healthy_at: float | None = None

    def start(self) -> None:
        if self._healthy_at is None:
            self._healthy_at = time.monotonic() + START_SECONDS

    def describe_target_health(self, **_arguments: str) -> dict[str, Any]:
        if self._healthy_at is None:
            return {"TargetHealthDescriptions": []}
        state = "healthy" if time.monotonic() >= self._healthy_at else "initial"
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": state}}]}


class Scaler:
    """The Lambda client, for the local stack: an invocation of the scaler starts the pool's target."""

    def __init__(self, targets: Targets) -> None:
        self._targets = targets

    def invoke(self, **_arguments: Any) -> dict[str, Any]:
        self._targets.start()
        return {"StatusCode": 202}


@contextmanager
def _moto() -> Generator[None]:
    """moto in place of AWS, one call at a time: the gateway calls the SDK from several threads,
    and moto's tables are plain dictionaries."""
    one_at_a_time = threading.Lock()
    make_api_call = BaseClient._make_api_call

    def call(self: BaseClient, operation: str, arguments: Mapping[str, Any]) -> Any:
        with one_at_a_time:
            return make_api_call(self, operation, arguments)

    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("AWS_ACCESS_KEY_ID", "testing")
        environment.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        environment.setenv("AWS_DEFAULT_REGION", _REGION)
        environment.delenv("AWS_PROFILE", raising=False)
        with mock_aws(), mock.patch.object(BaseClient, "_make_api_call", call):
            yield


def _gateway_environment(pool: str) -> dict[str, str]:
    """The environment of a gateway of the one pool, over tables and a secret created in moto."""
    dynamodb = boto3.client("dynamodb")
    for table, name in _TABLES.values():
        keys = [(table.partition_key, "HASH")] + ([(table.sort_key, "RANGE")] if table.sort_key else [])
        dynamodb.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": key.name, "KeyType": kind} for key, kind in keys],
            AttributeDefinitions=[{"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys],
            BillingMode="PAY_PER_REQUEST",
        )
    secret = boto3.client("secretsmanager").create_secret(Name=f"pool-{pool}", SecretString=secrets.token_urlsafe(24))
    target_group = f"arn:aws:elasticloadbalancing:{_REGION}:{_ACCOUNT}:targetgroup/{pool}/0123456789abcdef"
    return {
        "RHI_POOLS": json.dumps({pool: _POOL}),
        "RHI_DEFAULT_POOL": pool,
        "RHI_UPSTREAM_URL": "http://internal.example.com",
        "RHI_TARGET_GROUPS": json.dumps({pool: target_group}),
        "RHI_POOL_SECRETS": json.dumps({pool: secret["ARN"]}),
        "RHI_SCALER_FUNCTION": "smoke-scaler",
        "RHI_COURSE_END": "2027-01-15",
        "RHI_KEEPALIVE_SECONDS": str(KEEPALIVE_SECONDS),
        **{variable: name for variable, (_, name) in _TABLES.items()},
    }


def book(pool: str, settings: Settings) -> Session:
    """A session of one instance booked on the pool for the hour that starts now, written to the
    pool-state table as every writer writes one (`rhi_common.sessions.started`)."""
    now = int(time.time())
    session = sessions.started(
        None,
        pool=pool,
        instances=1,
        starts_at=now,
        ends_at=now + sessions.HOUR_SECONDS,
        set_by="smoke",
        now=now,
        max_instances=next(configured for configured in settings.pools if configured.name == pool).max_instances,
        session_max_hours=settings.session_max_hours,
    )
    boto3.resource("dynamodb").Table(settings.pool_state_table).put_item(Item=session.to_item())
    return session


def enrol(github_id: int = 0, alias: str = "canary") -> str:
    """A key drawn now for one student, its item written to the keys table as the issuer writes the
    canary's. While the local stack is served, a test that needs more students than the canary
    enrols each under a GitHub ID of its own (`tests/unit/spikes/test_loadgen.py`)."""
    key, now = generate_key(), int(time.time())
    item = StudentKey(
        key_hash=key_hash(key),
        github_id=github_id,
        alias=alias,
        installation=None,
        issued_at=now - 60,
        expires_at=now + 86_400,
        revoked_at=None,
        concurrency_limit=2,
    ).to_item()
    boto3.resource("dynamodb").Table(_TABLES["RHI_KEYS_TABLE"][1]).put_item(Item=item)
    return key


@contextmanager
def local_stack(pool: str) -> Generator[tuple[str, str]]:
    """The local stack of the one pool, served while the context lasts: its address and the canary's key."""
    with _moto():
        environ = _gateway_environment(pool)
        key = enrol()
        settings = read_settings(environ)
        session = book(pool, settings)
        clients = aws_clients()
        pool_secret = clients.secretsmanager.get_secret_value(SecretId=settings.pool_secrets[pool])["SecretString"]
        targets = Targets()
        gateway = build(
            settings,
            replace(clients, elbv2=targets, lambda_client=Scaler(targets)),
            transport=httpx.ASGITransport(app=create_app(MockSettings(secret=pool_secret))),
        )
        # The lifespan runs the readiness reads and the heartbeat: without it there is no gateway.
        server = uvicorn.Server(uvicorn.Config(gateway.app, lifespan="on", access_log=False, log_level="warning"))
        with socket.socket() as listening:
            listening.bind(("127.0.0.1", 0))
            address = f"http://127.0.0.1:{listening.getsockname()[1]}"
            serving = threading.Thread(target=server.run, kwargs={"sockets": [listening]}, name="smoke-gateway")
            serving.start()
            try:
                deadline = time.monotonic() + 30
                # Started, and its first read of the pool's records done: the session is the one booked above.
                while not (server.started and gateway.readiness.pool(pool).session == session):
                    assert serving.is_alive() and time.monotonic() < deadline, "the local gateway did not start"
                    time.sleep(0.01)
                yield address, key
            finally:
                server.should_exit = True
                serving.join(timeout=30)


@pytest.fixture(scope="session")
def read_stack_target() -> Callable[[Mapping[str, str]], Target]:
    """`read_target`, for the test of what the environment asks for."""
    return read_target


@pytest.fixture(scope="module")
def stack() -> Generator[Stack]:
    """The stack under test: the endpoint the environment gives, or the local stack."""
    target = read_target(os.environ)
    timeout = httpx.Timeout(connect=10.0, read=READ_SECONDS, write=30.0, pool=10.0)
    # Nothing of the machine's own configuration, a proxy among it, takes part in a request.
    with httpx.Client(timeout=timeout, trust_env=False) as http:
        if target.endpoint is not None and target.key is not None:
            yield Stack(endpoint=target.endpoint, key=target.key, pool=target.pool, deployed=True, http=http)
            return
        with local_stack(target.pool) as (address, key):
            yield Stack(endpoint=address, key=key, pool=target.pool, deployed=False, http=http)
