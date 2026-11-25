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
  tests run;
- the mock engine (`rhi_mock_engine.app`) as the transport of the gateway's hop to its pool;
- `Targets` for the target group's health: the pool has no target until its session is booked,
  and its target is healthy `START_SECONDS` later.

**The session.** The gateway serves a pool only in its session, the canary's key like any other
(section 5.6), and a request starts nothing (section 5.3). So the local stack starts as a deployed
one does, with a pool nobody has booked: `unbooked_stack` gives it so, and its `book` does what
the verification's booking and the scaler do on a deployed stack. It writes a session on the pool
for the hour that starts then, and the scaler's note of one instance starting; the target is
healthy a moment later, and `book` returns once the gateway reads the pool ready. `local_stack`
gives the stack booked and ready, to whoever needs only to send it requests.

The fixture `stack` gives the stack as the environment names it, and the fixture `booked` the
same stack with the pool under test in a session: on the local stack it books the pool, and on a
deployed stack it does nothing, because the verification booked the pool before the run
(`scripts/pipeline/verify.py`). The one test of a pool nobody has booked takes `stack` alone, and
runs before any test that takes `booked`.

**A stream that one task does not serve yet.** A deployed gateway is several tasks, and each reads
its pools on its own, every 5 seconds: the target group's health, then the scaler's note, then the
session (`rhi_gateway.readiness`). `GET /status` is one task's answer: when it first reads the pool
ready, which is when whoever booked the pool starts the tests, another task may still hold an older
view, and answers a request 503 from it until its next read, with any of the three causes of a pool
that cannot serve (`NOT_READY`, the causes of `PoolView.refusal`):

- `warming`: it has read neither the healthy target nor a newer note;
- `capacity_unavailable`: its read of the health came before the target was healthy, and its read
  of the note after the scaler counted the instance in service, so none starting;
- `no_session`: it has not read the session yet, on a pool whose target was healthy when the
  session was booked.

So `Stack.stream` makes a request again while the answer is one of those, for `SETTLE_SECONDS` at
most, three of those reads. It gives the answer it ends on: a stream test still needs a whole
stream, and a pool that cannot serve by then fails it. No other answer is asked again, and only a
stream is: the test of the pool nobody has booked makes one request and reads its answer.

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
from dataclasses import dataclass, field, replace
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
from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.records import ScalerNote, Session
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

# How long after its session is booked the local pool's target is healthy.
START_SECONDS = 1.0
# How long the local stack's booking waits for its gateway to read the pool ready: the gateway
# reads its pools every 5 seconds.
READY_SECONDS = 30.0
# How long the client waits for the next bytes of an answer: an engine is silent while it computes
# a long transcript.
READ_SECONDS = 120.0
# What a gateway task answers, each as a 503, for a pool its view says cannot serve
# (`rhi_gateway.readiness.PoolView.refusal`).
NOT_READY = frozenset({Cause.NO_SESSION, Cause.WARMING, Cause.CAPACITY_UNAVAILABLE})
# How long a stream is asked again while its answer is one of those, since the first such answer,
# and between two requests: the gateway's tasks do not read a pool at the same moment, and are at
# most one read of 5 seconds apart.
SETTLE_SECONDS = 15.0
AGAIN_SECONDS = 1.0

# Placeholders, which moto never checks.
_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_TABLES = {
    "RHI_KEYS_TABLE": (tables.KEYS, "smoke-keys"),
    "RHI_USAGE_TABLE": (tables.USAGE, "smoke-usage"),
    "RHI_HEARTBEAT_TABLE": (tables.HEARTBEATS, "smoke-heartbeats"),
    "RHI_POOL_STATE_TABLE": (tables.POOL_STATE, "smoke-pool-state"),
}
# The local pool: the mock engine.
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
    """The deployed-only tests run after the others, whatever their folder's name: what both stacks
    answer is asserted first."""
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
    """The stack under test, and how a test reaches it. `deployed` is whether an endpoint was given.

    `book` books the pool under test and returns once it reads ready; it is the local stack's, and
    does nothing on a deployed stack, whose pool the verification books.
    """

    endpoint: str
    key: str
    pool: str
    deployed: bool
    http: httpx.Client
    book: Callable[[], None] = field(default=lambda: None, repr=False, compare=False)

    def request(self, method: str, path: str, *, keyed: bool = True, **arguments: Any) -> httpx.Response:
        """One request to the endpoint, with the canary's key unless `keyed` is false."""
        headers = {"Authorization": f"Bearer {self.key}"} if keyed else {}
        return self.http.request(method, self.endpoint + path, headers=headers, **arguments)

    def stream(self, path: str, body: Mapping[str, Any]) -> Stream:
        """A streamed request with the canary's key, read to the end of its answer. While the
        answer is 503 with a cause of `NOT_READY`, the request is made again every `AGAIN_SECONDS`,
        for `SETTLE_SECONDS` at most since the first such answer: the answer given is the last."""
        until: float | None = None
        while True:
            answer, cause = self._streamed(path, body)
            if answer.status != 503 or cause not in NOT_READY:
                return answer
            until = time.monotonic() + SETTLE_SECONDS if until is None else until
            if time.monotonic() + AGAIN_SECONDS > until:
                return answer
            time.sleep(AGAIN_SECONDS)

    def _streamed(self, path: str, body: Mapping[str, Any]) -> tuple[Stream, str | None]:
        """One streamed request, read to the end of its answer, and the cause the answer names when
        it is an error and no stream: its `error.type`, in every wire format."""
        reader, events, read = SseReader(), [], b""
        headers = {"Authorization": f"Bearer {self.key}"}
        with self.http.stream("POST", self.endpoint + path, headers=headers, json=body) as answer:
            for chunk in answer.iter_bytes():
                events += reader.feed(chunk)
                read += b"" if answer.status_code == 200 else chunk
        try:
            cause = json.loads(read)["error"]["type"]
        except (ValueError, KeyError, TypeError):
            cause = None
        return Stream(status=answer.status_code, events=tuple(events)), cause if isinstance(cause, str) else None


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
        **{variable: name for variable, (_, name) in _TABLES.items()},
    }


def book(pool: str, settings: Settings) -> Session:
    """A session of one instance booked on the pool for the hour that starts now, written to the
    pool-state table as every writer writes one (`rhi_common.sessions.started`), with the note the
    scaler writes once it has asked for that instance: one starting."""
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
    table = boto3.resource("dynamodb").Table(settings.pool_state_table)
    table.put_item(Item=session.to_item())
    # What the scaler's next run writes of a pool it has just asked an instance for (section 5.4).
    starting = ScalerNote(pool, capacity_asked=1, no_capacity_at=None, written_at=now, requested=1, starting=1)
    table.put_item(Item=starting.to_item())
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


@dataclass(frozen=True)
class Local:
    """The local stack while it is served: its address, the canary's key, and `book`, which books
    the pool and returns once the gateway reads it ready. Booking it again does nothing."""

    address: str
    key: str
    book: Callable[[], None]


@contextmanager
def unbooked_stack(pool: str) -> Generator[Local]:
    """The local stack of the one pool, served while the context lasts, with the pool unbooked: a
    request for it is answered `no_session` until `Local.book` is called."""
    with _moto():
        environ = _gateway_environment(pool)
        key = enrol()
        settings = read_settings(environ)
        clients = aws_clients()
        pool_secret = clients.secretsmanager.get_secret_value(SecretId=settings.pool_secrets[pool])["SecretString"]
        targets = Targets()
        gateway = build(
            settings,
            replace(clients, elbv2=targets),
            transport=httpx.ASGITransport(app=create_app(MockSettings(secret=pool_secret))),
        )
        # The lifespan runs the readiness reads: without it there is no gateway.
        server = uvicorn.Server(uvicorn.Config(gateway.app, lifespan="on", access_log=False, log_level="warning"))
        booked: list[Session] = []

        def book_the_pool() -> None:
            if booked:
                return
            booked.append(book(pool, settings))
            # The instance the session asks for: in service a moment later.
            targets.start()
            deadline = time.monotonic() + READY_SECONDS
            while not ((view := gateway.readiness.pool(pool)).session == booked[0] and view.ready):
                assert serving.is_alive() and time.monotonic() < deadline, "the local pool did not turn ready"
                time.sleep(0.01)

        with socket.socket() as listening:
            listening.bind(("127.0.0.1", 0))
            address = f"http://127.0.0.1:{listening.getsockname()[1]}"
            serving = threading.Thread(target=server.run, kwargs={"sockets": [listening]}, name="smoke-gateway")
            serving.start()
            try:
                deadline = time.monotonic() + 30
                # Started, and its first read of the pool done.
                while not (server.started and gateway.readiness.pool(pool).observed_at is not None):
                    assert serving.is_alive() and time.monotonic() < deadline, "the local gateway did not start"
                    time.sleep(0.01)
                yield Local(address, key, book_the_pool)
            finally:
                server.should_exit = True
                serving.join(timeout=30)


@contextmanager
def local_stack(pool: str) -> Generator[tuple[str, str]]:
    """The local stack of the one pool, booked and ready, served while the context lasts: its
    address and the canary's key. A request for the pool is forwarded from the first."""
    with unbooked_stack(pool) as local:
        local.book()
        yield local.address, local.key


@pytest.fixture(scope="session")
def read_stack_target() -> Callable[[Mapping[str, str]], Target]:
    """`read_target`, for the test of what the environment asks for."""
    return read_target


@pytest.fixture(scope="module")
def stack() -> Generator[Stack]:
    """The stack under test: the endpoint the environment gives, or the local stack, whose pool
    nobody has booked until a test takes `booked`."""
    target = read_target(os.environ)
    timeout = httpx.Timeout(connect=10.0, read=READ_SECONDS, write=30.0, pool=10.0)
    # Nothing of the machine's own configuration, a proxy among it, takes part in a request.
    with httpx.Client(timeout=timeout, trust_env=False) as http:
        if target.endpoint is not None and target.key is not None:
            yield Stack(endpoint=target.endpoint, key=target.key, pool=target.pool, deployed=True, http=http)
            return
        with unbooked_stack(target.pool) as local:
            yield Stack(
                endpoint=local.address, key=local.key, pool=target.pool, deployed=False, http=http, book=local.book
            )


@pytest.fixture(scope="module")
def booked(stack: Stack) -> Stack:
    """The stack with the pool under test in a session. The local stack's pool is booked here, and
    is ready a moment later; a deployed stack's pool was booked by whoever runs the tests."""
    stack.book()
    return stack
