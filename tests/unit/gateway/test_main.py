"""The gateway's entry point (`rhi_gateway.main`) and its image (`services/gateway/Dockerfile`).

The gateway is built as `main` builds it, from an environment, over moto in place of DynamoDB and
Secrets Manager: the four tables are created with the keys of `rhi_common.tables`, and each pool
has a secret whose value is drawn when the tests run, like the students' keys. Nothing reaches
AWS: the credentials and the region are placeholders that moto never checks. The target groups'
health is a small double where a test needs a pool that is ready, and the engines are an
`httpx.MockTransport`.

The shutdown is tested on a real process: this file, run as a script, serves the gateway through
`serve` on a Unix socket, over moto, and the test sends it SIGTERM. The Dockerfile is read, not
built: starting the image is the container tests' part.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import boto3
import httpx
import pytest
from botocore.client import BaseClient
from moto import mock_aws

from rhi_common import tables
from rhi_common.causes import Cause
from rhi_common.keys import generate_key, key_hash
from rhi_common.records import SESSION_RECORD, ScalerNote, Session
from rhi_common.students import StudentKey
from rhi_gateway import main as entry
from rhi_gateway import metrics
from rhi_gateway.main import Gateway, StartError, aws_clients, build, serve, task_id
from rhi_gateway.settings import SettingsError, read_settings

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "services/gateway/Dockerfile"

ACCOUNT = "123456789012"
REGION = "us-east-1"
TASK = "0123456789abcdef0123456789abcdef"
UPSTREAM = "http://internal.example.com:8000"
SCALER = "scaler-of-the-test"
# The variable of each table, the table's keys and the name it is deployed under in the tests.
TABLES = {
    "RHI_KEYS_TABLE": (tables.KEYS, "keys-of-the-test"),
    "RHI_USAGE_TABLE": (tables.USAGE, "usage-of-the-test"),
    "RHI_HEARTBEAT_TABLE": (tables.HEARTBEATS, "heartbeats-of-the-test"),
    "RHI_POOL_STATE_TABLE": (tables.POOL_STATE, "pool-state-of-the-test"),
}
KEYS_TABLE, USAGE_TABLE, HEARTBEAT_TABLE = (TABLES[name][1] for name in list(TABLES)[:3])
POOL = {
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

# A Messages request for the pool `mock`, and the engine's whole answer to it.
MESSAGE = {"model": "mock", "max_tokens": 16, "messages": [{"role": "user", "content": "Hello"}]}
ANSWER = {
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "Hello."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 5},
}


def target_group(pool: str) -> str:
    return f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{pool}/0123456789abcdef"


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def as_logged(error: BaseException) -> str:
    """The error as the task's log shows it: its message, and those of the errors it is chained to."""
    return "".join(traceback.format_exception(error))


# --- moto in place of AWS ------------------------------------------------------------------------


@contextmanager
def moto_aws(record: Callable[[str, Mapping[str, Any]], None] | None = None) -> Generator[None]:
    """moto in place of AWS, one call at a time, each told to `record` first.

    The gateway calls the SDK from several threads (the readiness reads, the ledger's writes,
    the lookups), and moto's tables are plain dictionaries that two threads cannot change at once.
    """
    one_at_a_time = threading.Lock()
    # The one place every call of every client goes through.
    make_api_call = BaseClient._make_api_call

    def call(self: BaseClient, operation: str, arguments: Mapping[str, Any]) -> Any:
        with one_at_a_time:
            if record is not None:
                record(operation, arguments)
            return make_api_call(self, operation, arguments)

    with mock_aws(), mock.patch.object(BaseClient, "_make_api_call", call):
        yield


def create_tables() -> None:
    dynamodb = boto3.client("dynamodb")
    for table, name in TABLES.values():
        keys = [(table.partition_key, "HASH")] + ([(table.sort_key, "RANGE")] if table.sort_key else [])
        dynamodb.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": key.name, "KeyType": kind} for key, kind in keys],
            AttributeDefinitions=[{"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys],
            BillingMode="PAY_PER_REQUEST",
        )


def environment(*pools: str) -> tuple[dict[str, str], dict[str, str]]:
    """The environment of a gateway of these pools, the first the default one, and the secret moto holds for each.

    Each pool is booked for the hour that starts now: the student of these tests is served on it (section 5.6).
    """
    now, pool_state = int(time.time()), boto3.resource("dynamodb").Table(TABLES["RHI_POOL_STATE_TABLE"][1])
    for pool in pools:
        booked = Session(pool, instances=1, starts_at=now, ends_at=now + 3600, set_by="owner", set_at=now)
        pool_state.put_item(Item=booked.to_item())
    secretsmanager = boto3.client("secretsmanager")
    values = {pool: secrets.token_urlsafe(24) for pool in pools}
    arns = {
        pool: secretsmanager.create_secret(Name=f"pool-{pool}", SecretString=value)["ARN"]
        for pool, value in values.items()
    }
    environ = {
        "RHI_POOLS": json.dumps(dict.fromkeys(pools, POOL)),
        "RHI_UPSTREAM_URL": UPSTREAM,
        "RHI_TARGET_GROUPS": json.dumps({pool: target_group(pool) for pool in pools}),
        "RHI_POOL_SECRETS": json.dumps(arns),
        "RHI_SCALER_FUNCTION": SCALER,
        "RHI_COURSE_END": "2027-01-15",
        **{variable: name for variable, (_, name) in TABLES.items()},
    }
    if pools:
        environ["RHI_DEFAULT_POOL"] = pools[0]
    return environ, values


def issue_key(key: str | None = None) -> str:
    """A student's key, its item written to the keys table as the issuer writes it."""
    key = key or generate_key()
    now = int(time.time())
    item = StudentKey(
        key_hash=key_hash(key),
        github_id=7,
        alias="ada",
        installation=None,
        issued_at=now - 60,
        expires_at=now + 86_400,
        revoked_at=None,
        concurrency_limit=2,
    ).to_item()
    boto3.resource("dynamodb").Table(KEYS_TABLE).put_item(Item=item)
    return key


def items(table: str) -> list[dict[str, Any]]:
    return boto3.resource("dynamodb").Table(table).scan()["Items"]


class Targets:
    """`describe_target_health`, in the test: each target group in `healthy` has one healthy target."""

    def __init__(self, *healthy: str) -> None:
        self._healthy = set(healthy)

    def describe_target_health(self, **arguments: str) -> dict[str, Any]:
        state = "healthy" if arguments["TargetGroupArn"] in self._healthy else "unused"
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": state}}]}


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv(entry.METADATA_VARIABLE, raising=False)
    with moto_aws():
        create_tables()
        yield


def started(gateway: Gateway, scenario: Callable[[httpx.AsyncClient], Awaitable[None]]) -> None:
    """Start the application (its lifespan), run `scenario` with a client of it, and stop it."""

    async def run() -> None:
        app = gateway.app
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://gateway.example.com") as client,
        ):
            await scenario(client)

    asyncio.run(asyncio.wait_for(run(), timeout=30))


# --- The application, built from an environment ----------------------------------------------------


@pytest.mark.usefixtures("aws")
def test_the_application_built_from_an_environment_answers_from_the_tables_moto_holds() -> None:
    environ, _ = environment("mock")
    key = issue_key()
    gateway = build(read_settings(environ), aws_clients(), task=TASK)

    async def scenario(client: httpx.AsyncClient) -> None:
        assert (await client.head("/api/hello")).status_code == 200

        without_a_key = await client.get("/v1/models")
        assert without_a_key.status_code == 401
        assert without_a_key.json()["error"]["type"] == "invalid_key"
        # A well-formed key the keys table does not hold is looked up there, and refused the same.
        unknown = await client.get("/v1/models", headers=bearer(generate_key()))
        assert (unknown.status_code, unknown.json()["error"]["type"]) == (401, "invalid_key")

        # The key store is the keys table: the key issued there is let in.
        listed = await client.get("/v1/models", headers=bearer(key))
        assert listed.status_code == 200
        assert ([model["id"] for model in listed.json()["data"]], listed.json()["default"]) == (["mock"], "mock")

    started(gateway, scenario)

    # The gateway ran and stopped, and wrote no row to the heartbeats table.
    assert items(HEARTBEAT_TABLE) == []


@pytest.mark.usefixtures("aws")
def test_a_gateway_whose_table_holds_no_session_for_its_pool_refuses_a_student_with_no_session() -> None:
    environ, _ = environment("mock")
    pool_state = boto3.resource("dynamodb").Table(TABLES["RHI_POOL_STATE_TABLE"][1])
    pool_state.delete_item(Key={"pool": "mock", "record": SESSION_RECORD})
    key = issue_key()
    gateway = build(read_settings(environ), aws_clients(), task=TASK)

    async def scenario(client: httpx.AsyncClient) -> None:
        refused = await client.post("/v1/messages", headers=bearer(key), json=MESSAGE)
        assert (refused.status_code, refused.json()["error"]["type"]) == (503, "no_session")
        assert "retry-after" not in refused.headers

    started(gateway, scenario)
    assert not items(USAGE_TABLE)


@pytest.mark.usefixtures("aws")
def test_a_gateway_with_no_pool_and_no_default_pool_starts_and_answers_the_hello() -> None:
    environ, _ = environment()
    assert (environ["RHI_POOLS"], environ["RHI_TARGET_GROUPS"], environ["RHI_POOL_SECRETS"]) == ("{}", "{}", "{}")
    assert "RHI_DEFAULT_POOL" not in environ
    key = issue_key()
    gateway = build(read_settings(environ), aws_clients(), task=TASK)

    async def scenario(client: httpx.AsyncClient) -> None:
        assert (await client.head("/api/hello")).status_code == 200

        listed = (await client.get("/v1/models", headers=bearer(key))).json()
        assert (listed["data"], listed["default"]) == ([], None)
        assert (await client.get("/status", headers=bearer(key))).json() == {"pools": []}
        # No pool, so no pool's session is active: a request is refused before its body is read, whatever it names.
        for model in ("claude-sonnet-4-5", "mock"):
            refused = await client.post("/v1/messages", headers=bearer(key), json={**MESSAGE, "model": model})
            assert (refused.status_code, refused.json()["error"]["type"]) == (503, "no_session")

    started(gateway, scenario)

    assert items(HEARTBEAT_TABLE) == []


@pytest.mark.usefixtures("aws")
def test_a_request_is_forwarded_with_the_secret_read_at_start_and_the_settings_of_the_environment() -> None:
    environ, pool_secrets = environment("mock", "second")
    environ |= {
        "RHI_BODY_LIMIT_BYTES": "512",
        "RHI_LONGEST_REQUEST_SECONDS": "900",
        "RHI_RETRY_AFTER_WARMING": "31",
        "RHI_TARGET_TTFT_P95_SECONDS": "6",
        "RHI_TARGET_STREAM_MEDIAN_TOKENS_PER_SECOND": "16.5",
    }
    key = issue_key()
    sent: list[httpx.Request] = []

    def engine(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=ANSWER)

    # The pool `mock` has a healthy target; `second` has none, and the scaler's note counts one instance starting.
    starting = ScalerNote("second", capacity_asked=1, no_capacity_at=None, written_at=int(time.time()), starting=1)
    boto3.resource("dynamodb").Table(TABLES["RHI_POOL_STATE_TABLE"][1]).put_item(Item=starting.to_item())
    clients = replace(aws_clients(), elbv2=Targets(target_group("mock")))
    gateway = build(read_settings(environ), clients, task=TASK, transport=httpx.MockTransport(engine))

    async def scenario(client: httpx.AsyncClient) -> None:
        # The application runs the readiness reads: nothing else here refreshes them.
        while not gateway.readiness.pool("mock").ready:
            await asyncio.sleep(0.01)

        answer = await client.post("/v1/messages", headers=bearer(key), json=MESSAGE)
        assert (answer.status_code, answer.json()) == (200, ANSWER)
        [request] = sent
        assert str(request.url) == UPSTREAM + "/v1/messages"
        # The bearer is the value Secrets Manager holds under the pool's ARN, which no variable held.
        assert request.headers["Authorization"] == "Bearer " + pool_secrets["mock"]
        assert pool_secrets["mock"] not in "".join(environ.values())
        # The ledger is the usage table's: the request's slot was taken and released there, and
        # nothing was charged.
        assert [(int(day["github_id"]), "tokens" in day, day["leases"]) for day in items(USAGE_TABLE)] == [
            (7, False, {})
        ]

        # The body limit of the environment: a body above it is refused, and nothing is forwarded.
        long = {**MESSAGE, "messages": [{"role": "user", "content": "x" * 600}]}
        too_long = await client.post("/v1/messages", headers=bearer(key), json=long)
        assert (too_long.status_code, too_long.json()["error"]["type"]) == (400, "request_refused")
        assert len(sent) == 1

        # The Retry-After of the environment: a request, and a token count, for a pool that is starting.
        count = {"model": "second", "messages": MESSAGE["messages"]}
        for path, body in (("/v1/messages", {**MESSAGE, "model": "second"}), ("/v1/messages/count_tokens", count)):
            warming = await client.post(path, headers=bearer(key), json=body)
            assert (warming.status_code, warming.json()["error"]["type"]) == (503, "warming")
            assert warming.headers["Retry-After"] == "31"
        assert len(sent) == 1

        # The gateway has no wake: the path is outside its table, with a key as without one.
        wake = await client.post("/wake", headers=bearer(key), json={"model": "second"})
        assert (wake.status_code, wake.json()["error"]["type"]) == (404, "not_found")

    started(gateway, scenario)

    # The setting no quick request shows is that of the part it governs.
    assert gateway.forwarding.proxy.longest_request_seconds == 900
    assert gateway.forwarding.retry_after == {
        Cause.WARMING: 31,
        Cause.CAPACITY_UNAVAILABLE: 60,
        Cause.CONCURRENCY_LIMIT: 5,
    }
    # The floors of the environment are boundaries of what students experience, beside the gateway's own.
    experience = gateway.app.state.experience
    experience.first_token.labels("mock", "messages", "warm", "first").observe(1)
    experience.speed.labels("mock", "messages", "warm").observe(1)
    bounds: dict[str, set[str]] = {}
    for family in gateway.metrics.registry.collect():
        for sample in family.samples:
            if sample.name.endswith("_bucket"):
                bounds.setdefault(sample.name, set()).add(sample.labels["le"])
    assert {"5.0", "6.0"} <= bounds["rhi_gateway_time_to_first_token_seconds_bucket"]
    assert {"10.0", "15.0", "16.5"} <= bounds["rhi_gateway_stream_output_tokens_per_second_bucket"]


# --- The entry point ---------------------------------------------------------------------------------


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """What `main` asked to be served, with nothing listening: the application and how, and the metrics."""
    asked: dict[str, list[Any]] = {"application": [], "metrics": []}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **how: asked["application"].append((app, how)))
    monkeypatch.setattr(metrics.Metrics, "serve", lambda self, **where: asked["metrics"].append(where))
    return asked


@pytest.mark.usefixtures("aws")
def test_main_serves_the_application_on_port_8080_and_the_metrics_on_the_loopback_port_9091(
    monkeypatch: pytest.MonkeyPatch, served: dict[str, list[Any]]
) -> None:
    environ, _ = environment("mock")
    for variable, value in environ.items():
        monkeypatch.setenv(variable, value)

    entry.main()

    [(app, how)] = served["application"]
    assert (how["host"], how["port"]) == ("0.0.0.0", 8080)
    # No deadline cuts a request in flight at SIGTERM, and the lifespan, where the pools are read, runs.
    assert how["timeout_graceful_shutdown"] is None
    assert how["lifespan"] == "on"
    # The metrics endpoint takes its port only: its address is the loopback one, and no caller's to give.
    assert served["metrics"] == [{"port": 9091}]
    assert metrics.HOST == "127.0.0.1"

    # What is served is the gateway of the process's environment.
    async def hello() -> int:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            return (await client.head("/api/hello")).status_code

    assert asyncio.run(hello()) == 200


@pytest.mark.usefixtures("aws")
def test_a_refused_setting_or_an_unread_secret_stops_the_gateway_before_anything_listens(
    monkeypatch: pytest.MonkeyPatch, served: dict[str, list[Any]]
) -> None:
    environ, pool_secrets = environment("mock")
    for variable, value in environ.items():
        monkeypatch.setenv(variable, value)

    monkeypatch.delenv("RHI_KEYS_TABLE")
    with pytest.raises(SettingsError, match="RHI_KEYS_TABLE"):
        entry.main()
    monkeypatch.setenv("RHI_KEYS_TABLE", KEYS_TABLE)

    # An ARN that names no secret: the error names the pool, and repeats neither the ARN nor AWS's text.
    missing = json.loads(environ["RHI_POOL_SECRETS"])["mock"] + "-gone"
    monkeypatch.setenv("RHI_POOL_SECRETS", json.dumps({"mock": missing}))
    with pytest.raises(StartError, match="'mock'") as raised:
        entry.main()
    logged = as_logged(raised.value)
    assert missing not in logged
    assert ACCOUNT not in logged
    assert "Secrets Manager can" not in logged  # the service's own sentence
    assert pool_secrets["mock"] not in logged

    assert served == {"application": [], "metrics": []}


def test_the_task_s_id_comes_from_the_task_metadata_and_is_random_without_it() -> None:
    asked: list[str] = []
    answers: list[httpx.Response] = []

    def metadata(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return answers.pop(0)

    environ = {"ECS_CONTAINER_METADATA_URI_V4": "http://metadata.example.com/v4/container-of-the-test"}
    transport = httpx.MockTransport(metadata)

    # The ID is the last part of the task's ARN: the account and the cluster are not in it.
    arn = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/cluster-of-the-test/{TASK}"
    answers.append(httpx.Response(200, json={"Cluster": "cluster-of-the-test", "TaskARN": arn}))
    assert task_id(environ, transport=transport) == TASK
    assert asked == ["http://metadata.example.com/v4/container-of-the-test/task"]

    # With no endpoint, the ID is random, and nothing is asked.
    random = [task_id({}, transport=transport), task_id({}, transport=transport)]
    assert len(asked) == 1
    # An endpoint that gives no task ID leaves a random one too: the gateway starts still.
    answers += [httpx.Response(500), httpx.Response(200, json=[]), httpx.Response(200, text="no JSON")]
    answers.append(httpx.Response(200, json={"TaskARN": "an ARN with spaces"}))
    random += [task_id(environ, transport=transport) for _ in range(4)]
    assert answers == []
    assert len(set(random)) == len(random)
    assert all(re.fullmatch(r"[0-9a-f]{32}", name) for name in random)


# --- The shutdown ------------------------------------------------------------------------------------

# The files of the folder the test and its child share, and the variables the test gives the child.
SOCKET, CALLS, FORWARDED, RELEASE, LOG = "gateway.sock", "calls.jsonl", "forwarded", "release", "gateway.log"
KEY_VARIABLE, METRICS_PORT_VARIABLE = "TEST_STUDENT_KEY", "TEST_METRICS_PORT"


def serve_until_sigterm(folder: Path) -> None:
    """The child of the shutdown test: the gateway of two pools, served as `main` serves it, over moto.

    The pool `mock` is ready and `second` is not. The engine says, with a file, that a request
    reached it, and answers once the test's own file is there. Every call of the SDK is written to
    a file as it is made: after its shutdown uvicorn raises the signal again, so nothing here runs
    after `serve`.
    """
    calls = (folder / CALLS).open("a", encoding="utf-8")

    def record(operation: str, arguments: Mapping[str, Any]) -> None:
        call: dict[str, Any] = {"operation": operation, "table": arguments.get("TableName")}
        calls.write(json.dumps(call) + "\n")
        calls.flush()

    async def engine(request: httpx.Request) -> httpx.Response:
        (folder / FORWARDED).touch()
        while not (folder / RELEASE).exists():
            await asyncio.sleep(0.02)
        return httpx.Response(200, json=ANSWER)

    with moto_aws(record):
        create_tables()
        environ, _ = environment("mock", "second")
        issue_key(os.environ[KEY_VARIABLE])
        clients = replace(aws_clients(), elbv2=Targets(target_group("mock")))
        gateway = build(read_settings(environ), clients, task=TASK, transport=httpx.MockTransport(engine))
        serve(gateway, metrics_port=int(os.environ[METRICS_PORT_VARIABLE]), uds=str(folder / SOCKET))


def until(condition: Callable[[], bool], child: subprocess.Popen[bytes], log: Path, what: str) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        assert child.poll() is None, log.read_text(encoding="utf-8")
        assert time.monotonic() < deadline, f"{what}: {log.read_text(encoding='utf-8')}"
        time.sleep(0.02)


def test_sigterm_stops_new_connections_and_finishes_the_request_in_flight() -> None:
    key = generate_key()
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        metrics_port = free.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="rhi-gateway-") as name:
        folder = Path(name)
        socket_path, log = folder / SOCKET, folder / LOG
        environ = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(REPO / "services/gateway"), str(REPO / "services/common")]),
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": REGION,
            KEY_VARIABLE: key,
            METRICS_PORT_VARIABLE: str(metrics_port),
        }
        environ.pop("AWS_PROFILE", None)
        environ.pop(entry.METADATA_VARIABLE, None)
        with log.open("wb") as output:
            child = subprocess.Popen([sys.executable, __file__, name], env=environ, stdout=output, stderr=output)

        def client() -> httpx.Client:
            transport = httpx.HTTPTransport(uds=str(socket_path))
            return httpx.Client(transport=transport, base_url="http://gateway", headers=bearer(key), timeout=30)

        def answers_the_hello(asking: httpx.Client) -> bool:
            try:
                return socket_path.exists() and asking.head("/api/hello").status_code == 200
            except httpx.TransportError:
                return False

        def refuses_connections() -> bool:
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.connect(str(socket_path))
                except (ConnectionRefusedError, FileNotFoundError):
                    return True
            return False

        answered: list[httpx.Response | Exception] = []

        def ask() -> None:
            try:
                with client() as asking:
                    answered.append(asking.post("/v1/messages", json=MESSAGE))
            except httpx.HTTPError as error:
                answered.append(error)

        in_flight = threading.Thread(target=ask)
        try:
            with client() as before:
                until(lambda: answers_the_hello(before), child, log, "the gateway did not start")
                # The entry point serves the metrics on the loopback address.
                scraped = httpx.get(f"http://127.0.0.1:{metrics_port}/metrics", timeout=10, trust_env=False)
                assert scraped.status_code == 200
                assert "rhi_gateway_student_requests_total" in scraped.text

                def mock_is_ready() -> bool:
                    states = {pool["model"]: pool["state"] for pool in before.get("/status").json()["pools"]}
                    return states["mock"] == "ready"

                until(mock_is_ready, child, log, "the pool was not read as ready")

            in_flight.start()
            until((folder / FORWARDED).exists, child, log, "the request did not reach the engine")
            child.send_signal(signal.SIGTERM)
            until(refuses_connections, child, log, "the gateway still takes connections")
            # The request the engine has not answered is still in flight, and the gateway waits for it.
            assert child.poll() is None
            assert answered == []
            (folder / RELEASE).touch()
            in_flight.join(timeout=30)
            child.wait(timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            if in_flight.ident is not None:
                in_flight.join(timeout=30)
        calls = [json.loads(line) for line in (folder / CALLS).read_text(encoding="utf-8").splitlines()]
        logged = log.read_text(encoding="utf-8")

    # The gateway ended on its own, once the request had: uvicorn then raises the signal again.
    assert child.returncode in (0, -signal.SIGTERM), logged
    assert "Traceback" not in logged
    [answer] = answered
    assert isinstance(answer, httpx.Response), logged
    assert (answer.status_code, answer.json()) == (200, ANSWER)

    # From its start to its end the gateway served a request, and it called AWS for its tables, its
    # secrets and nothing else: no row of the heartbeats table, and no call of the scaler function.
    assert USAGE_TABLE in {call["table"] for call in calls}
    assert HEARTBEAT_TABLE not in {call["table"] for call in calls if call["operation"] != "CreateTable"}
    assert "Invoke" not in {call["operation"] for call in calls}


# --- The image -----------------------------------------------------------------------------------

_PINNED = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")


def arguments(text: str, keyword: str) -> list[str]:
    """The arguments of each instruction of a Dockerfile with that keyword, as written, continuation lines joined."""
    lines = (line.strip() for line in re.sub(r"\\\n", " ", text).splitlines())
    return [line[len(keyword) :].strip() for line in lines if line.upper().startswith(keyword + " ")]


def instructions(text: str, keyword: str) -> list[list[str]]:
    """The same, each split into words."""
    return [shlex.split(written) for written in arguments(text, keyword)]


def base_images(text: str) -> list[str]:
    return [next(word for word in words if not word.startswith("--")) for words in instructions(text, "FROM")]


def unpinned(text: str) -> list[str]:
    """The base images that are not pinned by a digest."""
    return [image for image in base_images(text) if not _PINNED.fullmatch(image)]


def test_the_base_image_is_pinned() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert base_images(text)
    assert unpinned(text) == []
    # The same check refuses the same file with its digest taken away.
    stripped = re.sub(r"@sha256:[0-9a-f]{64}", "", text)
    assert unpinned(stripped) == base_images(stripped) != []


def test_the_image_is_built_from_its_build_context_with_hashes_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    [install] = [words for words in instructions(text, "RUN") if "install" in words]
    assert {"--require-hashes", "--no-deps"} <= set(install)
    assert install[install.index("--requirement") + 1] == "requirements.txt"
    # The entry point, in exec form: Python is process 1, so SIGTERM reaches it.
    [command] = arguments(text, "CMD")
    assert json.loads(command) == ["python", "-m", "rhi_gateway.main"]
    assert instructions(text, "EXPOSE") == [["8080"]]

    # The packaging module imports the CDK, whose jsii refuses a package cache below a directory
    # another user owns, as in the implement loop's sandbox; without the cache it needs none.
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    from rhi import packaging

    # The build context of a Python service: the Dockerfile copies the requirements, the shared
    # package and the gateway's from it, and nothing that is not there.
    context = Path(packaging.image_context("gateway", DOCKERFILE))
    copied = [[word for word in words[:-1] if not word.startswith("--")] for words in instructions(text, "COPY")]
    assert sorted(source.rstrip("/") for sources in copied for source in sources) == [
        "requirements.txt",
        "rhi_common",
        "rhi_gateway",
    ]
    assert all((context / source).exists() for sources in copied for source in sources)
    assert (context / "rhi_gateway/main.py").is_file()
    exported = re.sub(r"\\\n", " ", (context / "requirements.txt").read_text(encoding="utf-8"))
    requirements = [line for line in exported.splitlines() if "==" in line]
    assert any(line.startswith("uvicorn==") for line in requirements)
    assert all("--hash=sha256:" in line for line in requirements)


if __name__ == "__main__":
    serve_until_sigterm(Path(sys.argv[1]))
