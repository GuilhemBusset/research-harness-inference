"""The agent's duties while its instance serves (`rhi_lifecycle.serving` and `rhi_lifecycle.metrics`;
system design, sections 4.1 and 4.3).

The engine is the mock engine, in process: the agent's requests reach it through a stand-in for the
network, which gives no answer once the request's time limit has passed on the test's clock. Three
engines stand for what the check of liveness must tell apart: the mock that hangs, whose health
route answers while it counts no token; `Busy`, slow but alive, whose token counts grow while it
answers no completion in time; and `DEAD`, where nothing listens on the engine's port. The
heartbeats table, the pool's secret and the host module are stand-ins too. The clock and the sleep
are the test's, so no test waits for a heartbeat or for a minute; the one socket is the agent's own
metrics endpoint, on a free port of the loopback interface.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import math
import urllib.request
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from botocore.exceptions import ClientError
from prometheus_client import CollectorRegistry
from prometheus_client.parser import text_string_to_metric_families
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from rhi_common.engine_metrics import MOCK, VLLM, Quantity, metric_names
from rhi_common.records import InstanceHeartbeat
from rhi_lifecycle import launch, metrics, serving
from rhi_lifecycle.host import Host
from rhi_lifecycle.release import Release, Settings
from rhi_mock_engine.app import Settings as MockSettings
from rhi_mock_engine.app import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine

    from starlette.requests import Request

NOW = 1_800_000_000
# Made-up values, built here: none is an instance, a credential or a commit.
INSTANCE = "-".join(["i", "0" * 17])
SECRET = "-".join(["the", "pool", "secret", "of", "this", "test"])
DIGEST = "sha256:" + "a" * 64
REVISION = "c" * 40
SERVED = Release(
    engine_image=f"registry.example.com/rhi/engine@{DIGEST}",
    model_revision=REVISION,
    engine_flags=("--max-model-len", "65536"),
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
CHAT = "/v1/chat/completions"
RUNNING = metric_names(MOCK)[Quantity.RUNNING_REQUESTS]
WAITING = metric_names(MOCK)[Quantity.WAITING_REQUESTS]
# A counter's sample carries the family's name with `_total` after it.
INPUT_TOKENS = metric_names(MOCK)[Quantity.INPUT_TOKENS] + "_total"
OUTPUT_TOKENS = metric_names(MOCK)[Quantity.OUTPUT_TOKENS] + "_total"
RESTARTS = serving.RESTARTS_METRIC + "_total"
# A stand-in engine that is dead: nothing listens on the engine's port, so no route gives an answer.
DEAD = None


class Clock:
    """Seconds since the epoch, moved only by the test."""

    def __init__(self) -> None:
        self.now = float(NOW)
        self._timers: list[tuple[float, asyncio.Future[None]]] = []

    def time(self) -> float:
        return self.now

    def timer(self, seconds: float) -> asyncio.Future[None]:
        """Done once the clock has moved by `seconds`. It counts from this call, not from a later turn of the loop."""
        timer = asyncio.get_running_loop().create_future()
        self._timers.append((self.now + seconds, timer))
        return timer

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for at, timer in self._timers:
            if at <= self.now and not timer.done():
                timer.set_result(None)
        await asyncio.sleep(0)


class Busy:
    """A stand-in engine that is slow but alive, as under long contexts with requests queued. Its
    health route answers 200. Its `/metrics` gives requests running and waiting and the tokens it
    has counted, under the mock's names: a thousand of input and fifty of output for every second
    of the test's clock, until `stalls_at`, from when it counts none. With `engine` vLLM, it gives
    them as vLLM does: under vLLM's names, each family typed and each series labelled by the model.
    A completion waits its turn and gets no answer, unless `answers` says the engine answers it.
    While `metrics` is false the metrics route gives no metrics."""

    def __init__(self, clock: Clock, *, stalls_at: float = math.inf, answers: bool = False, engine: str = MOCK) -> None:
        self._clock = clock
        self._stalls_at = stalls_at
        self._answers = answers
        self._engine = engine
        self.metrics = True
        self.app = Starlette(
            routes=[
                Route("/health", self._health),
                Route("/metrics", self._metrics),
                Route(CHAT, self._complete, methods=["POST"]),
            ]
        )

    async def _health(self, request: Request) -> Response:
        return Response()

    async def _metrics(self, request: Request) -> Response:
        if not self.metrics:
            return Response(status_code=503)
        worked = min(self._clock.now, self._stalls_at) - NOW
        if self._engine == MOCK:
            lines = [
                f"{RUNNING} 4.0",
                f"{WAITING} 3.0",
                f"{INPUT_TOKENS} {1000 * worked}",
                f"{OUTPUT_TOKENS} {50 * worked}",
            ]
            return Response("\n".join(lines) + "\n")
        # vLLM's own names, written out rather than taken from the engine's row, so that a wrong
        # name in the row fails the test. Its counters are typed under their samples' names.
        assert self._engine == VLLM
        labels = '{engine="0",model_name="coursework"}'
        lines = [
            "# TYPE vllm:num_requests_running gauge",
            f"vllm:num_requests_running{labels} 4.0",
            "# TYPE vllm:num_requests_waiting gauge",
            f"vllm:num_requests_waiting{labels} 3.0",
            "# TYPE vllm:prompt_tokens_total counter",
            f"vllm:prompt_tokens_total{labels} {1000 * worked}",
            "# TYPE vllm:generation_tokens_total counter",
            f"vllm:generation_tokens_total{labels} {50 * worked}",
        ]
        return Response("\n".join(lines) + "\n")

    async def _complete(self, request: Request) -> Response:
        if not self._answers:
            await asyncio.Event().wait()  # behind the queue until the agent hangs up
        return JSONResponse({"choices": [{"message": {"role": "assistant", "content": "Hi"}}]})


class World(Host):
    """An instance in service, and everything around its agent: the mock engine, the heartbeats
    table, Secrets Manager and the host. Made inside the test's event loop."""

    def __init__(self, engine: MockSettings | None = None, *, restarted: MockSettings | None = None) -> None:
        super().__init__(ecr=object())
        self.loop = asyncio.get_running_loop()
        self.clock = Clock()
        # While the gate is closed, a request the mock runs is not answered: it stays in flight.
        self.gate = asyncio.Event()
        self.gate.set()
        # The engine now (`DEAD`: nothing listens), and the one a restart of the unit starts.
        self.app: Starlette | None = self.mock(engine or MockSettings(secret=SECRET))
        self.restarted = restarted or MockSettings(secret=SECRET)
        self.restarts = 0
        self.secret_readable = True
        self.tables: list[str] = []
        self.items: list[dict[str, Any]] = []
        # The completions the agent asked the engine for, and how often a duty went to sleep.
        self.asked: list[dict[str, Any]] = []
        self.naps = 0
        self.closed = False

    def mock(self, settings: MockSettings) -> Starlette:
        async def pace(seconds: float) -> None:
            await self.gate.wait()

        return create_app(settings, clock=self.clock.time, sleep=pace)

    def agent(
        self, served: Release = SERVED, registry: CollectorRegistry | None = None, engine: str = MOCK
    ) -> serving.Serving:
        return serving.Serving(
            served,
            SETTINGS,
            INSTANCE,
            engine,
            dynamodb=self,
            secrets=self,
            host=self,
            fetch=self.fetch,
            registry=registry or CollectorRegistry(),
            clock=self.clock.time,
            sleep=self.sleep,
        )

    @contextlib.asynccontextmanager
    async def doing(self, duty: Coroutine[Any, Any, None]) -> AsyncGenerator[None]:
        """Run a duty of the agent beside the test, and stop it when the test is done."""
        task = asyncio.ensure_future(duty)
        try:
            yield
        finally:
            self.closed = True
            task.cancel()
            # A duty that ended by itself raises here what ended it.
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def sleep(self, seconds: float) -> None:
        self.naps += 1
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        await self.clock.timer(seconds)

    @property
    def rows(self) -> list[InstanceHeartbeat]:
        return [InstanceHeartbeat.from_item(item) for item in self.items]

    # The heartbeats table, Secrets Manager and the host module.

    def Table(self, name: str) -> World:
        self.tables.append(name)
        return self

    def put_item(self, Item: dict[str, Any]) -> None:
        self.items.append(Item)

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        if request != {"SecretId": SETTINGS.secret_id} or not self.secret_readable:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetSecretValue")
        return {"SecretString": SECRET}

    def restart_engine(self) -> None:
        self.restarts += 1
        self.app = self.mock(self.restarted)

    # The engine, where the agent opens a URL. The agent calls from a worker thread.

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        answer = asyncio.run_coroutine_threadsafe(self._answer(request, timeout), self.loop)
        return answer.result(timeout=10)  # a guard, not the clock under test

    async def _answer(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        if self.closed or self.app is None:
            return None
        assert timeout is not None, "every request of these duties has a time limit"
        assert request.full_url.startswith(launch.ENGINE_URL)
        path = request.full_url.removeprefix(launch.ENGINE_URL)
        content = request.data if isinstance(request.data, bytes) else None
        if path == CHAT and content is not None:
            self.asked.append(json.loads(content))
        limit = self.clock.timer(timeout)
        call = asyncio.ensure_future(
            self.ask(request.get_method(), path, headers=dict(request.header_items()), content=content)
        )
        await asyncio.wait({call, limit}, return_when=asyncio.FIRST_COMPLETED)
        if not call.done():
            call.cancel()  # the time limit has passed: the agent hangs up
            return None
        limit.cancel()
        return call.result().status_code, call.result().content

    async def ask(self, method: str, path: str, **request: Any) -> httpx.Response:
        """One request to the engine, from the instance itself."""
        assert self.app is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=launch.ENGINE_URL) as http:
            return await http.request(method, path, **request)

    async def reported(self) -> tuple[float, float]:
        """The running and the waiting requests, as the engine's own `/metrics` gives them now."""
        lines = (await self.ask("GET", "/metrics")).text.splitlines()
        values = dict(line.rsplit(" ", 1) for line in lines if not line.startswith("#"))
        return float(values[RUNNING]), float(values[WAITING])

    async def tokens(self) -> float | None:
        """The tokens the engine has counted, as the agent reads them in the engine's `/metrics` now."""
        return serving.read_tokens((await self.ask("GET", "/metrics")).content, MOCK)


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    # Bounded, so that a duty that stalls fails its test and does not stall the run.
    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


async def until(condition: Callable[[], bool]) -> None:
    """Wait for what a worker thread or another task does. A guard, not the clock under test."""

    async def polled() -> None:
        while not condition():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(polled(), timeout=10)


async def minute(world: World, agent: serving.Serving) -> bool:
    """One minute of the agent's duties, each made by the test when it is due, so that their order
    is the test's: the check, a heartbeat every 5 seconds from the same moment, and whether the
    check restarted the engine. A completion with no answer fails 30 seconds in, after the
    heartbeat of the 25th second and before the one of the 30th."""
    asked = len(world.asked)
    check = asyncio.ensure_future(asyncio.to_thread(agent.check))
    await until(lambda: check.done() or len(world.asked) > asked)  # the completion is sent, or none will be
    for _ in range(6):
        await asyncio.to_thread(agent.beat)
        await world.clock.advance(5)
    restarted = await asyncio.wait_for(check, timeout=10)
    for _ in range(6):
        await asyncio.to_thread(agent.beat)
        await world.clock.advance(5)
    return restarted


def test_the_heartbeat_carries_the_engines_load_and_the_release_every_5_seconds() -> None:
    async def scenario() -> None:
        # An engine that runs two requests at once, and three students: one request waits.
        world = World(MockSettings(secret=SECRET, max_running=2))
        world.gate.clear()
        body = {"model": SETTINGS.pool, "messages": [{"role": "user", "content": "Hello"}]}
        students = [
            asyncio.ensure_future(world.ask("POST", CHAT, headers={"Authorization": f"Bearer {SECRET}"}, json=body))
            for _ in range(3)
        ]
        while await world.reported() != (2, 1):
            await asyncio.sleep(0)

        async with world.doing(world.agent().report()):
            await until(lambda: world.naps == 1)
            first = InstanceHeartbeat(
                instance=INSTANCE,
                pool=SETTINGS.pool,
                running=2,
                waiting=1,
                image_digest=DIGEST,
                model_revision=REVISION,
                stage=serving.STAGE,
                written_at=NOW,
            )
            assert world.rows == [first]
            assert world.tables == [SETTINGS.heartbeat_table]

            await world.clock.advance(4)
            assert world.rows == [first]  # nothing before the 5 seconds have passed
            await world.clock.advance(1)
            await until(lambda: world.naps == 2)
            assert world.rows == [first, replace(first, written_at=NOW + 5)]

            # The students are answered: the next row says the engine is idle.
            world.gate.set()
            assert [answer.status_code for answer in await asyncio.gather(*students)] == [200, 200, 200]
            await world.clock.advance(5)
            await until(lambda: world.naps == 3)
            assert world.rows[2:] == [replace(first, running=0, waiting=0, written_at=NOW + 10)]

    run(scenario)


@pytest.mark.parametrize("revision", [REVISION, None])
def test_the_release_metric_is_served_and_says_what_the_heartbeat_says(revision: str | None) -> None:
    async def scenario() -> None:
        world = World()
        registry = CollectorRegistry()
        agent = world.agent(replace(SERVED, model_revision=revision), registry)
        assert await asyncio.to_thread(agent.beat)
        (row,) = world.rows

        server = metrics.serve(registry, port=0)
        try:
            assert server.server_address[0] == "127.0.0.1"  # the instance itself, and nobody else
            here = f"http://127.0.0.1:{server.server_port}"
            scrape = launch.fetch(urllib.request.Request(here + metrics.ROUTE), 5)
            elsewhere = launch.fetch(urllib.request.Request(here + "/"), 5)
        finally:
            server.shutdown()
            server.server_close()

        assert scrape is not None
        assert scrape[0] == 200
        families = text_string_to_metric_families(scrape[1].decode())
        release = [sample for family in families for sample in family.samples if sample.name == serving.RELEASE_METRIC]
        # One sample, whose labels are the two the heartbeat carries. A pool with no model says so in both.
        assert [sample.labels for sample in release] == [
            {"image_digest": row.image_digest, "model_revision": row.model_revision}
        ]
        assert (row.image_digest, row.model_revision) == (DIGEST, revision or serving.NO_MODEL)
        assert elsewhere is not None
        assert elsewhere[0] == 404

    run(scenario)
    # Where the node's collector scrapes: the agent starts the endpoint with these.
    where = inspect.signature(metrics.serve).parameters
    assert (where["host"].default, where["port"].default, metrics.ROUTE) == ("127.0.0.1", 9110, "/metrics")


def test_no_row_is_written_while_the_engine_gives_no_load() -> None:
    async def scenario() -> None:
        world = World()
        agent = world.agent()
        engine, world.app = world.app, DEAD  # nothing listens, as during the engine's restart

        assert not await asyncio.to_thread(agent.beat)
        assert world.items == []

        world.app = engine
        assert await asyncio.to_thread(agent.beat)
        assert [(row.running, row.waiting) for row in world.rows] == [(0, 0)]

    run(scenario)


def test_the_load_is_read_under_the_engines_names_and_summed_over_its_series() -> None:
    # An engine that labels its series, as vLLM does by model.
    labelled = f'{RUNNING}{{model_name="a"}} 2.0\n{RUNNING}{{model_name="b"}} 1.0\n{WAITING}{{model_name="a"}} 4.0\n'

    assert serving.read_load(labelled.encode(), MOCK) == (3, 4)
    assert serving.read_load(f"{RUNNING} 2.0\n".encode(), MOCK) is None  # no waiting requests reported
    assert serving.read_load(b"\xff not an exposition", MOCK) is None


def test_every_minute_the_engine_answers_a_completion_of_one_token() -> None:
    async def scenario() -> None:
        world = World()
        async with world.doing(world.agent().watch()):
            await until(lambda: world.naps == 1)
            assert len(world.asked) == 1

            await world.clock.advance(59)
            assert len(world.asked) == 1
            await world.clock.advance(1)
            await until(lambda: world.naps == 2)

        # The mock answers only a request that carries the pool's secret, and an answer is no reason to restart.
        assert [(body["model"], body["max_tokens"]) for body in world.asked] == [(SETTINGS.pool, 1)] * 2
        assert world.restarts == 0

    run(scenario)


def test_the_tokens_are_read_under_the_engines_names_and_summed() -> None:
    async def scenario() -> None:
        world = World()
        assert await world.tokens() == 0
        body = {"model": SETTINGS.pool, "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 1}
        answer = await world.ask("POST", CHAT, headers={"Authorization": f"Bearer {SECRET}"}, json=body)
        # What the mock engine counted for one request, input and output, is what the agent reads.
        usage = answer.json()["usage"]
        assert usage["prompt_tokens"] > 0
        assert usage["completion_tokens"] > 0
        assert await world.tokens() == usage["prompt_tokens"] + usage["completion_tokens"]

    run(scenario)
    labelled = f'{INPUT_TOKENS}{{model_name="a"}} 7.0\n{INPUT_TOKENS}{{model_name="b"}} 2.0\n{OUTPUT_TOKENS} 4.0\n'
    assert serving.read_tokens(labelled.encode(), MOCK) == 13
    assert serving.read_tokens(f"{INPUT_TOKENS} 7.0\n".encode(), MOCK) is None  # no output tokens reported
    assert serving.read_tokens(b"\xff not an exposition", MOCK) is None


def test_a_hung_engine_is_restarted() -> None:
    async def scenario() -> None:
        # Inference hangs while health answers (evidence E23).
        world = World(MockSettings(secret=SECRET, hang=True))
        assert await asyncio.to_thread(launch.Engine(world.fetch).healthy)
        registry = CollectorRegistry()

        async with world.doing(world.agent(registry=registry).run()):
            await until(lambda: len(world.asked) == 1)
            # The clock stops short of the minute. A heartbeat is written every 5 seconds of it,
            # also while the completion is unanswered.
            for row in range(1, 12):
                await until(lambda: len(world.rows) == row)  # noqa: B023 - called before the next turn
                if world.clock.now < NOW + serving.PROBE_SECONDS:
                    # Until its restart, the engine holds the completion and has counted no token.
                    assert world.restarts == 0
                    assert await world.tokens() == 0
                await world.clock.advance(5)
            # The check counts a restart once the host has made it: its count ends the wait.
            await until(lambda: registry.get_sample_value(RESTARTS) == 1)

        # The first check restarted it: the second is not due before the minute.
        assert world.clock.now < NOW + 60
        assert len(world.asked) == 1
        assert world.restarts == 1
        assert registry.get_sample_value(RESTARTS) == 1

    run(scenario)


def test_a_dead_engine_is_restarted() -> None:
    async def scenario() -> None:
        world = World()
        world.app = DEAD
        assert not await asyncio.to_thread(launch.Engine(world.fetch).healthy)
        registry = CollectorRegistry()

        async with world.doing(world.agent(registry=registry).run()):
            # The check counts a restart once the host has made it: its count ends the wait.
            await until(lambda: registry.get_sample_value(RESTARTS) == 1)

        # At its first check, with no wait: nothing was there to answer.
        assert world.clock.now == NOW
        assert world.restarts == 1
        assert registry.get_sample_value(RESTARTS) == 1

    run(scenario)


@pytest.mark.parametrize("engine", [MOCK, VLLM])
def test_a_busy_engine_that_answers_no_completion_in_time_is_not_restarted(engine: str) -> None:
    # Under the names of each engine: the mock's, and those of vLLM, the coursework pool's engine.
    async def scenario() -> None:
        world = World()
        world.app = Busy(world.clock, engine=engine).app
        registry = CollectorRegistry()

        async with world.doing(world.agent(registry=registry, engine=engine).run()):
            # Five minutes, 5 seconds at a time. Before each move of the clock the heartbeat that
            # is due is written and the completion that is due is sent. None is answered: each
            # fails 30 seconds after it was sent.
            for beat in range(60):
                await until(lambda: len(world.rows) == beat + 1 and len(world.asked) == beat // 12 + 1)  # noqa: B023 - called before the next turn
                await world.clock.advance(5)
            # The sixth completion is sent once the check that sent the fifth has ended.
            await until(lambda: len(world.asked) == 6)

        assert world.clock.now == NOW + 300
        assert world.restarts == 0
        assert registry.get_sample_value(RESTARTS) == 0
        # Every heartbeat read the load under the engine's names, and wrote its row.
        assert {(row.stage, row.running, row.waiting) for row in world.rows} == {(serving.STAGE, 4, 3)}

    run(scenario)


@pytest.mark.parametrize("engine", [MOCK, VLLM])
def test_an_engine_that_stopped_counting_tokens_is_restarted_a_minute_after_it_last_counted_one(engine: str) -> None:
    async def scenario() -> None:
        world = World()
        # The engine counts tokens for 5 seconds, then hangs: the heartbeat of the 5th second is
        # the last that reads more tokens than the one before it.
        world.app = Busy(world.clock, stalls_at=NOW + 5, engine=engine).app
        registry = CollectorRegistry()
        agent = world.agent(registry=registry, engine=engine)
        for _ in range(4):
            await asyncio.to_thread(agent.beat)
            await world.clock.advance(5)

        # The first completion is sent at 20 seconds and fails at 50, which is 45 seconds after the
        # engine last counted a token: longer than a completion may take, and within the minute.
        assert serving.PROBE_SECONDS < 45 < serving.PROBE_INTERVAL_SECONDS
        assert not await minute(world, agent)
        assert world.restarts == 0
        assert registry.get_sample_value(RESTARTS) == 0

        # The second fails at 110 seconds, 105 seconds after.
        assert await minute(world, agent)
        assert len(world.asked) == 2
        assert world.restarts == 1
        assert registry.get_sample_value(RESTARTS) == 1

    run(scenario)


def test_more_tokens_after_a_heartbeat_that_read_none_do_not_show_the_engine_works() -> None:
    async def scenario() -> None:
        world = World()
        # The engine hangs at 7 seconds. Its metrics give no answer at the heartbeat of the 5th
        # second, so the one of the 10th reads more tokens than the last that read any, at 0, and
        # has none of the heartbeat before it to compare them with.
        busy = Busy(world.clock, stalls_at=NOW + 7)
        world.app = busy.app
        agent = world.agent()
        for answered in (True, False, True):
            busy.metrics = answered
            await asyncio.to_thread(agent.beat)
            await world.clock.advance(5)

        assert await minute(world, agent)
        assert len(world.asked) == 1
        assert world.restarts == 1

    run(scenario)


@pytest.mark.parametrize("tokens", ["grow", "stand still", "cannot be read"])
def test_an_engine_that_answers_its_completion_is_not_restarted_whatever_its_tokens_do(tokens: str) -> None:
    async def scenario() -> None:
        world = World()
        busy = Busy(world.clock, stalls_at=NOW if tokens == "stand still" else math.inf, answers=True)
        busy.metrics = tokens != "cannot be read"
        world.app = busy.app
        registry = CollectorRegistry()
        agent = world.agent(registry=registry)

        assert [await minute(world, agent) for _ in range(5)] == [False] * 5
        assert len(world.asked) == 5
        assert world.restarts == 0
        assert registry.get_sample_value(RESTARTS) == 0

    run(scenario)


def test_a_restarted_engine_is_checked_again_only_once_it_is_healthy() -> None:
    async def scenario() -> None:
        # After its restart the engine takes 100 seconds to load, as a model does.
        world = World(MockSettings(secret=SECRET, hang=True), restarted=MockSettings(secret=SECRET, start_seconds=100))
        async with world.doing(world.agent().watch()):
            await until(lambda: len(world.asked) == 1)
            await world.clock.advance(60)
            await until(lambda: world.naps == 2)
            assert world.restarts == 1

            # A minute later the engine is still loading: no request is sent to it, which would
            # restart it again.
            await world.clock.advance(60)
            await until(lambda: world.naps == 3)
            assert len(world.asked) == 1

            await world.clock.advance(60)
            await until(lambda: world.naps == 4)
            assert len(world.asked) == 2

        assert world.restarts == 1

    run(scenario)


def test_without_the_secret_the_engine_is_not_checked_and_not_restarted() -> None:
    async def scenario() -> None:
        world = World()
        world.secret_readable = False
        async with world.doing(world.agent().watch()):
            await until(lambda: world.naps == 1)

        assert world.asked == []
        assert world.restarts == 0

    run(scenario)
