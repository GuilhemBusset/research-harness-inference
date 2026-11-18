"""The gateway's metrics per student, and the endpoint the collector scrapes (`rhi_gateway.metrics`).

The gateway runs whole over the in-memory key store and ledger; the engine is an
`httpx.MockTransport` that reports the usage the test sets. The counters are read from the
gateway's registry, as a scrape reads them. Every key is generated when the tests run.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families
from prometheus_client.samples import Sample
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import parse_pools
from rhi_common.students import StudentKey
from rhi_gateway import metrics, proxy, routes
from rhi_gateway.app import Forwarding, Holding, build_app, forwarding_handlers
from rhi_gateway.auth import Authenticator
from rhi_gateway.body import BodyFilter
from rhi_gateway.budget import InMemoryLedger, Student
from rhi_gateway.formats import messages, responses
from rhi_gateway.formats.chat import ChatBodyFilter
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.readiness import PoolView, Readiness
from rhi_gateway.waker import Waker

ROOT = Path(__file__).resolve().parents[3]
NOW = 1_800_000_000
POOL = {
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
POOLS = parse_pools({"coursework": POOL, "research": POOL})
# The students of these tests: the alias, the GitHub ID, and the tokens of a day.
OTTER = ("otter", 600_600_601, 2_000_000)
HERON = ("heron", 600_600_707, 500_000)
REQUESTS, INPUT, OUTPUT, ERRORS = (
    f"{name}_total" for name in (metrics.REQUESTS, metrics.INPUT_TOKENS, metrics.OUTPUT_TOKENS, metrics.ERRORS)
)


def body(entry: routes.Route, pool: str = "coursework") -> dict[str, Any] | None:
    """A body the route accepts, for that pool; none for a route that reads none."""
    said = [{"role": "user", "content": "hello"}]
    return {
        routes.MESSAGES: {"model": pool, "max_tokens": 100, "messages": said},
        routes.COUNT_TOKENS: {"model": pool, "messages": said},
        routes.CHAT_COMPLETIONS: {"model": pool, "messages": said},
        routes.RESPONSES: {"model": pool, "input": "hello"},
        routes.WAKE: {"model": pool},
    }.get(entry)


class Aws:
    """The AWS clients the readiness and the waker are given. It answers these three calls and has no other."""

    def __init__(self) -> None:
        self.calls: set[str] = set()

    def describe_target_health(self, **_: Any) -> dict[str, Any]:
        self.calls.add("describe_target_health")
        return {"TargetHealthDescriptions": []}

    def get_item(self, **_: Any) -> dict[str, Any]:
        self.calls.add("get_item")
        return {}

    def invoke(self, **_: Any) -> dict[str, Any]:
        self.calls.add("invoke")
        return {"StatusCode": 202}


@dataclass
class Gateway:
    """The whole gateway, every route served, with its metrics; the key of each student, by alias."""

    app: Starlette
    metrics: metrics.Metrics
    keys: dict[str, str]
    aws: Aws
    # The usage the engine reports for the next request: input and output tokens.
    usage: list[int] = field(default_factory=lambda: [5000, 7])


def gateway(*students: tuple[str, int, int], ledger: Any = None) -> Gateway:
    keys = {alias: generate_key() for alias, _, _ in students}
    items = [
        StudentKey(
            key_hash=key_hash(keys[alias]),
            github_id=github_id,
            alias=alias,
            installation=None,
            issued_at=NOW - 3600,
            expires_at=NOW + 86400,
            revoked_at=None,
            daily_token_budget=budget,
            concurrency_limit=4,
        )
        for alias, github_id, budget in students
    ]
    ledger = InMemoryLedger(clock=lambda: float(NOW)) if ledger is None else ledger
    made = Gateway(Starlette(), metrics.Metrics(ledger), keys, Aws())

    def engine(request: httpx.Request) -> httpx.Response:
        # One answer every forwarded route reads as whole, with the usage under each format's names.
        used_in, used_out = made.usage
        usage = {
            "input_tokens": used_in,
            "output_tokens": used_out,
            "prompt_tokens": used_in,
            "completion_tokens": used_out,
        }
        return httpx.Response(200, json={"type": "message", "input_tokens": used_in, "usage": usage})

    authenticator = Authenticator(InMemoryKeyStore(*items), wall_clock=lambda: float(NOW))
    ready = lambda: {pool.name: PoolView(ready=True) for pool in POOLS}
    forwarding = Forwarding(
        authenticator=authenticator,
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
            {pool.name: "secret" for pool in POOLS},
        ),
        holding=Holding(views=ready, wake=lambda pool: None),
    )
    readiness = Readiness(
        {pool.name: f"tg-{pool.name}" for pool in POOLS}, elbv2=made.aws, table=made.aws, clock=lambda: float(NOW)
    )
    local = LocalRoutes(
        authenticator,
        POOLS,
        ready,
        default_pool=POOLS[0],
        clock=lambda: float(NOW),
        waker=Waker(readiness, lambda_client=made.aws, function_name="scaler", clock=lambda: float(NOW)),
        ledger=ledger,
    )
    made.app = build_app(forwarding_handlers(forwarding), local=local, metrics=made.metrics)
    return made


def call(app: Starlette, method: str, path: str, *, key: str | None = None, body: Any = None) -> int:
    """One request over ASGI: the status answered."""
    headers = [(b"content-type", b"application/json")]
    if key is not None:
        headers.append((b"x-api-key", key.encode()))
    request: list[Message] = [
        {"type": "http.request", "body": b"" if body is None else json.dumps(body).encode(), "more_body": False}
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        if request:
            return request.pop()
        await asyncio.get_running_loop().create_future()  # The client never leaves.
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("192.0.2.1", 50000),
        "server": ("gateway.example.com", 443),
    }
    asyncio.run(app(scope, receive, send))
    return next(message for message in sent if message["type"] == "http.response.start")["status"]


def samples(counted: metrics.Metrics, name: str | None = None) -> list[Sample]:
    """What a scrape reads: the samples of one name, or every sample.

    Every sample includes those prometheus-client adds to say when each counter's series began.
    """
    families = text_string_to_metric_families(generate_latest(counted.registry).decode())
    return [sample for family in families for sample in family.samples if name in (None, sample.name)]


def value(counted: metrics.Metrics, name: str, **labels: str) -> float | None:
    """The value of the one series of that name and exactly those labels; `None` when there is none."""
    return counted.registry.get_sample_value(name, labels)


def test_two_students_are_counted_apart(capsys: pytest.CaptureFixture[str]) -> None:
    made = gateway(OTTER, HERON)
    # The usage the engine reports for each request sent, by student, pool and route.
    sent = {
        ("otter", "coursework"): [(routes.MESSAGES, 5000, 7), (routes.CHAT_COMPLETIONS, 1200, 30)],
        ("otter", "research"): [(routes.RESPONSES, 800, 5)],
        ("heron", "coursework"): [(routes.MESSAGES, 40, 2)],
    }
    for (alias, pool), requests in sent.items():
        for entry, used_in, used_out in requests:
            made.usage = [used_in, used_out]
            assert call(made.app, entry.method, entry.path, key=made.keys[alias], body=body(entry, pool)) == 200

    for (alias, pool), requests in sent.items():
        assert value(made.metrics, REQUESTS, alias=alias, pool=pool) == len(requests)
        assert value(made.metrics, INPUT, alias=alias, pool=pool) == sum(used for _, used, _ in requests)
        assert value(made.metrics, OUTPUT, alias=alias, pool=pool) == sum(used for _, _, used in requests)
    # No series but these: nothing of one student is in a series of the other, or of a pool neither used.
    for name in (REQUESTS, INPUT, OUTPUT):
        assert {(sample.labels["alias"], sample.labels["pool"]) for sample in samples(made.metrics, name)} == set(sent)
    assert samples(made.metrics, ERRORS) == []

    # A clean end is charged the usage the engine reported, so that is what the day's budget has lost.
    for alias, _, budget in (OTTER, HERON):
        charged = sum(i + o for (student, _), requests in sent.items() if student == alias for _, i, o in requests)
        assert value(made.metrics, metrics.BUDGET_LEFT, alias=alias) == budget - charged
    assert len(samples(made.metrics, metrics.BUDGET_LEFT)) == 2
    # The log says the same of each request as the counters do.
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sum(record["input_tokens"] for record in logged) == 5000 + 1200 + 800 + 40


def test_students_are_aliases() -> None:
    made = gateway(OTTER)
    alias, github_id, _ = OTTER
    key = made.keys[alias]
    for entry in routes.TABLE:
        assert call(made.app, entry.method, entry.path, key=key, body=body(entry)) == 200
    # Refusals of an accepted key, one before the pool is known and one after: a body that is not JSON, an unknown model.
    assert call(made.app, "POST", "/v1/messages", key=key, body="not an object") == 400
    assert call(made.app, "POST", "/v1/chat/completions", key=key, body=body(routes.CHAT_COMPLETIONS, "nowhere")) == 400

    exposed = samples(made.metrics)
    of_a_student = [sample for sample in exposed if sample.labels.get("alias")]
    assert {sample.labels["alias"] for sample in of_a_student} == {alias}
    assert {sample.name for sample in of_a_student} >= {REQUESTS, INPUT, OUTPUT, ERRORS, metrics.BUDGET_LEFT}
    # A student has no other name in any series: no label but these three, and nowhere the ID or the key.
    assert {name for sample in exposed for name in sample.labels} == {"alias", "pool", "cause"}
    for sample in exposed:
        written = " ".join([sample.name, *sample.labels, *sample.labels.values()])
        assert str(github_id) not in written
        assert sample.value != github_id
    text = generate_latest(made.metrics.registry).decode()
    assert key not in text
    assert key_hash(key) not in text


def test_errors_by_cause() -> None:
    made = gateway(OTTER, ("heron", HERON[1], 50))
    # A student over the day's budget is refused once the body has named the pool.
    assert call(made.app, "POST", "/v1/messages", key=made.keys["heron"], body=body(routes.MESSAGES)) == 402
    assert value(made.metrics, ERRORS, alias="heron", pool="coursework", cause="budget_reached") == 1
    # A key nobody issued is refused before any student or pool is known.
    assert call(made.app, "POST", "/v1/messages", key=generate_key(), body=body(routes.MESSAGES)) == 401
    assert value(made.metrics, ERRORS, alias="", pool="", cause="invalid_key") == 1
    # A request the gateway answers with no cause is no error.
    assert call(made.app, "POST", "/v1/messages", key=made.keys["otter"], body=body(routes.MESSAGES)) == 200
    assert len(samples(made.metrics, ERRORS)) == 2
    assert sum(sample.value for sample in samples(made.metrics, REQUESTS)) == 3

    async def failing(request: Request, entry: routes.Route) -> Response:
        raise RuntimeError("the handler failed")

    faulty = metrics.Metrics()
    assert call(build_app({routes.MODELS: failing}, metrics=faulty), "GET", "/v1/models") == 500
    assert value(faulty, ERRORS, alias="", pool="", cause="none") == 1
    assert len(samples(faulty, ERRORS)) == 1
    assert value(faulty, REQUESTS, alias="", pool="") == 1


def test_every_request_answered_is_counted_once() -> None:
    made = gateway(OTTER)
    key = made.keys["otter"]
    answered: list[tuple[str, str, str | None, Any, int]] = [
        (entry.method, entry.path, key, body(entry), 200) for entry in routes.TABLE
    ]
    answered += [
        ("POST", "/v1/responses", generate_key(), body(routes.RESPONSES), 401),
        ("GET", "/v1/models", None, None, 401),
        ("POST", "/v1/messages", key, "not an object", 400),
        ("GET", "/v1/nowhere", key, None, 404),
        ("GET", "/v1/messages", key, None, 404),
    ]
    for count, (method, path, presented, sent, status) in enumerate(answered, start=1):
        assert call(made.app, method, path, key=presented, body=sent) == status
        assert sum(sample.value for sample in samples(made.metrics, REQUESTS)) == count
    # The local routes are the student's requests too: the models, the status, and here the body refused.
    assert value(made.metrics, REQUESTS, alias="otter", pool="") == 3
    # Of no student: the hello, the two refused keys, and the two paths outside the table, answered before any key is read.
    assert value(made.metrics, REQUESTS, alias="", pool="") == 5


def test_the_endpoint_serves_on_the_loopback_address_only() -> None:
    assert (metrics.HOST, metrics.PORT) == ("127.0.0.1", 9091)
    made = gateway(OTTER)
    assert call(made.app, "POST", "/v1/messages", key=made.keys["otter"], body=body(routes.MESSAGES)) == 200
    server = made.metrics.serve(port=0)  # A free port: another gateway may be running on this host.
    try:
        host, port = server.server_address[:2]
        # The one address the socket listens on.
        assert host == metrics.HOST
        with urllib.request.urlopen(f"http://{metrics.HOST}:{port}{metrics.ROUTE}", timeout=10) as answer:
            assert answer.headers["Content-Type"].startswith("text/plain")
            scraped = {
                (sample.name, sample.labels.get("alias")): sample.value
                for family in text_string_to_metric_families(answer.read().decode())
                for sample in family.samples
            }
        assert scraped[REQUESTS, "otter"] == 1
        assert scraped[metrics.BUDGET_LEFT, "otter"] == OTTER[2] - 5007
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"http://{metrics.HOST}:{port}/", timeout=10)
        assert refused.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_endpoint_takes_no_other_address() -> None:
    # Nothing a caller gives opens the endpoint outside the task: the address is not an argument.
    serve: Any = metrics.Metrics().serve
    with pytest.raises(TypeError):
        serve(host="0.0.0.0", port=0).server_close()


def test_a_budget_that_cannot_be_read_leaves_the_counters_served() -> None:
    class Unread(InMemoryLedger):
        def tokens_left(self, student: Student) -> int:
            raise OSError("the table did not answer")

    made = gateway(OTTER, ledger=Unread(clock=lambda: float(NOW)))
    assert call(made.app, "GET", "/v1/models", key=made.keys["otter"]) == 200
    assert value(made.metrics, REQUESTS, alias="otter", pool="") == 1
    assert samples(made.metrics, metrics.BUDGET_LEFT) == []


def test_the_gateway_publishes_no_cloudwatch_metric() -> None:
    made = gateway(OTTER)
    for entry in routes.TABLE:
        assert call(made.app, entry.method, entry.path, key=made.keys["otter"], body=body(entry)) == 200
    # Counting a request calls AWS for nothing: the doubles were asked what the wake asks, and they have no other call.
    assert made.aws.calls <= {"describe_target_health", "get_item", "invoke"}
    # No module of the gateway names the call that writes a CloudWatch metric, or the log format that makes one.
    writes_a_metric = re.compile(r"put_metric_data|PutMetricData|CloudWatchMetrics|[\"']_aws[\"']")
    sources = sorted((ROOT / "services" / "gateway" / "rhi_gateway").rglob("*.py"))
    assert (ROOT / "services" / "gateway" / "rhi_gateway" / "metrics.py") in sources
    assert [path.name for path in sources if writes_a_metric.search(path.read_text(encoding="utf-8"))] == []


def test_the_collector_scrapes_this_port() -> None:
    config = yaml.safe_load((ROOT / "services" / "gateway" / "collector" / "config.yaml").read_text(encoding="utf-8"))
    targets = [
        target
        for receiver in config["receivers"].values()
        for job in (receiver or {}).get("config", {}).get("scrape_configs", [])
        for static in job["static_configs"]
        for target in static["targets"]
    ]
    assert targets == [f"{metrics.HOST}:{metrics.PORT}"] == ["127.0.0.1:9091"]
