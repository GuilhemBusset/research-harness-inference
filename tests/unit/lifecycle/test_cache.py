"""The engine's cache capacity, as the agent reports it (`rhi_lifecycle.cache`; system design,
sections 4.7 and 7.2).

The engine is the mock engine, in process: the agent's heartbeat reaches it through a stand-in for
the network. The heartbeats table is a stand-in too. What the agent reports is read where the
node's collector reads it: the route of the agent's metrics endpoint, as its application answers.
"""

from __future__ import annotations

import asyncio
import urllib.request
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families
from prometheus_client.samples import Sample

from rhi_common.engine_metrics import MOCK, cache_capacity
from rhi_lifecycle import cache, launch, metrics, serving
from rhi_lifecycle.host import Host
from rhi_lifecycle.release import Release, Settings
from rhi_mock_engine.app import Settings as MockSettings
from rhi_mock_engine.app import create_app

# Made-up values, built here: none is an instance, a credential or a commit.
INSTANCE = "-".join(["i", "0" * 17])
SECRET = "-".join(["the", "pool", "secret", "of", "this", "test"])
SERVED = Release(
    engine_image="registry.example.com/rhi/engine@sha256:" + "a" * 64,
    model_revision="c" * 40,
    engine_flags=(),
    files_sha256="b" * 64,
)
SETTINGS = Settings(
    pool="coursework",
    group="rhi-coursework",
    launch_hook="launch",
    termination_hook="termination",
    log_group="/rhi/coursework",
    workspace_url="https://aps-workspaces.example.com/api/v1/remote_write",
    heartbeat_table="rhi-heartbeats",
    secret_id="rhi/coursework/engine",
)
CAPACITY = 1_500_000


def place() -> tuple[str, str]:
    """The information metric and the label where the mock's row says the mock gives its capacity."""
    where = cache_capacity(MOCK)
    assert where is not None
    return where.metric, where.label


class Instance:
    """An instance in service: the mock engine, the heartbeats table, and the agent with its
    registry. `engine` is None while nothing listens, and `alter` rewrites what the engine's
    `/metrics` answers before the agent gets it."""

    def __init__(self, engine: MockSettings | None = None) -> None:
        self.alter: Callable[[str], str] = lambda text: text
        self.app = create_app(engine) if engine is not None else None
        self.registry = CollectorRegistry()
        self.agent = serving.Serving(
            SERVED,
            SETTINGS,
            INSTANCE,
            MOCK,
            dynamodb=self,
            secrets=None,
            host=Host(ecr=object()),
            fetch=self.fetch,
            registry=self.registry,
        )

    def Table(self, name: str) -> Instance:
        return self

    def put_item(self, Item: dict[str, Any]) -> None:
        pass

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        app = self.app
        if app is None:
            return None
        assert request.full_url == f"{launch.ENGINE_URL}/metrics"

        async def ask() -> httpx.Response:
            # From the instance itself: the mock serves its metrics to a loopback client only.
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
            async with httpx.AsyncClient(transport=transport, base_url=launch.ENGINE_URL) as http:
                return await http.get("/metrics")

        answer = asyncio.run(ask())
        return answer.status_code, self.alter(answer.text).encode()

    def reported(self) -> list[Sample]:
        """Every sample of the capacity metric in what the agent's metrics endpoint answers now."""
        statuses: list[str] = []
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": metrics.ROUTE}
        body = b"".join(metrics.application(self.registry)(environ, lambda status, headers: statuses.append(status)))
        assert statuses == ["200 OK"]
        families = list(text_string_to_metric_families(body.decode()))
        named = [family for family in families if family.name == cache.METRIC]
        samples = [sample for family in families for sample in family.samples if sample.name.startswith(cache.METRIC)]
        # A family that is declared has a series, and a series is of the declared family.
        assert bool(named) == bool(samples)
        return samples


def without_the_metric(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if place()[0] not in line)


def with_the_label(written: str) -> Callable[[str], str]:
    def altered(text: str) -> str:
        assert f'"{CAPACITY}"' in text
        return text.replace(f'"{CAPACITY}"', f'"{written}"')

    return altered


def test_the_agent_reports_the_capacity_the_engine_gives_as_a_number() -> None:
    instance = Instance(MockSettings(secret=SECRET, cache_tokens=CAPACITY))
    assert instance.reported() == []  # nothing before the first reading

    assert instance.agent.beat()  # the heartbeat is written as before

    (sample,) = instance.reported()
    assert (sample.name, sample.labels, sample.value) == (cache.METRIC, {}, 1500000)


def test_no_capacity_no_series(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing listens.
    instance = Instance()
    assert not instance.agent.beat()
    assert instance.reported() == []

    # The engine answers, and its metrics do not carry the information metric, or carry no number.
    instance = Instance(MockSettings(secret=SECRET, cache_tokens=CAPACITY))
    for alter in (without_the_metric, *map(with_the_label, ("", "many", "nan", "inf", "0", "-1500000", "1500000.5"))):
        instance.alter = alter
        assert instance.agent.beat()  # the load is still read, and the heartbeat written
        assert instance.reported() == [], alter

    # The engine's row does not say where the engine gives its capacity.
    instance.alter = lambda text: text
    monkeypatch.setattr(cache, "cache_capacity", lambda engine: None)
    assert instance.agent.beat()
    assert instance.reported() == []


def test_the_series_goes_away_when_the_capacity_can_no_longer_be_read() -> None:
    instance = Instance(MockSettings(secret=SECRET, cache_tokens=CAPACITY))
    instance.agent.beat()
    assert [sample.value for sample in instance.reported()] == [CAPACITY]

    instance.app = None  # the engine restarts
    instance.agent.beat()
    assert instance.reported() == []

    instance.app = create_app(MockSettings(secret=SECRET, cache_tokens=2 * CAPACITY))
    instance.agent.beat()
    assert [sample.value for sample in instance.reported()] == [2 * CAPACITY]


def test_the_capacity_is_read_where_the_engines_row_says() -> None:
    metric, label = place()

    def answer(*series: str) -> bytes:
        return "".join([f"# TYPE {metric} gauge\n", *(f"{metric}{{{labels}}} 1\n" for labels in series)]).encode()

    assert cache.read_capacity(answer(f'{label}="1500000"'), MOCK) == CAPACITY
    # An engine that labels its series gives the capacity once per series.
    assert cache.read_capacity(answer(f'model="a",{label}="1500000"', f'model="b",{label}="1500000"'), MOCK) == CAPACITY
    # Two capacities are not one, another label is not the capacity, and the value 1 is not it either.
    assert cache.read_capacity(answer(f'model="a",{label}="1500000"', f'model="b",{label}="750000"'), MOCK) is None
    assert cache.read_capacity(answer('other_tokens="1500000"'), MOCK) is None
    assert cache.read_capacity(answer(), MOCK) is None
    assert cache.read_capacity(b"\xff not an exposition", MOCK) is None
    assert cache.read_capacity(b"{", MOCK) is None
    with pytest.raises(KeyError):
        cache.read_capacity(answer(f'{label}="1500000"'), "no-such-engine")
