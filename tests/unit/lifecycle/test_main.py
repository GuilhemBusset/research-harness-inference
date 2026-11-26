"""The lifecycle agent's program (`python -m rhi_lifecycle`; system design, section 4.3), run
against stand-ins for everything around it.

One `World` is the instance metadata and the engine, where the agent opens a URL; Auto Scaling,
the release parameter and the pool's secret, as SDK clients; the host module, with the console; and
the heartbeats table. The engine is the mock engine, in process, once the host module has started its unit. The
record of the release is in the test's own folder, and the clock is moved only by the test: the program's sleep, the
duties' and the transitions' all wait on it, so no test waits for a tick. The one socket is the
agent's own metrics endpoint, on a free port of the loopback interface.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import urllib.request
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from botocore.exceptions import ClientError
from prometheus_client import CollectorRegistry

from rhi_common.engine_metrics import MOCK, Quantity, metric_names
from rhi_common.records import InstanceHeartbeat
from rhi_lifecycle import __main__ as program
from rhi_lifecycle import launch, metrics, release, serving
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.termination import POLL_SECONDS
from rhi_mock_engine.app import Settings as MockSettings
from rhi_mock_engine.app import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine
    from pathlib import Path

    from starlette.applications import Starlette

NOW = 1_800_000_000
NAME = "/rhi/pools/coursework/release"
# Made-up values, built here: none is an instance or a credential.
INSTANCE = "-".join(["i", "0" * 17])
SECRET = "-".join(["the", "pool", "secret", "of", "this", "test"])

VALUE: dict[str, Any] = {
    "engine_image": f"registry.example.com/rhi/engine@sha256:{'a' * 64}",
    "model_revision": None,
    "engine_flags": [],
    "files": {"bucket": "artifacts", "key": "releases/coursework.tar", "sha256": "b" * 64},
    "settings": {
        "pool": "coursework",
        "group": "rhi-coursework",
        "launch_hook": "launch",
        "termination_hook": "termination",
        "log_group": "/rhi/coursework",
        "workspace_url": "https://aps-workspaces.example.com/api/v1/remote_write",
        "heartbeat_table": "rhi-heartbeats",
        "secret_id": "rhi/coursework/engine",
    },
}
# The variables of /run/rhi/release.env that the agent reads (services/engine/boot/rhi-boot.sh).
ENVIRON = {
    "RHI_RELEASE_PARAMETER": NAME,
    "RHI_REGION": "us-east-1",
    "RHI_POOL": "coursework",
    "RHI_GROUP": "rhi-coursework",
    "RHI_LAUNCH_HOOK": "launch",
    "RHI_TERMINATION_HOOK": "termination",
    "RHI_LOG_GROUP": "/rhi/coursework",
    "RHI_WORKSPACE_URL": "https://aps-workspaces.example.com/api/v1/remote_write",
    "RHI_HEARTBEAT_TABLE": "rhi-heartbeats",
    "RHI_SECRET_ID": "rhi/coursework/engine",
}

IN_SERVICE = "InService"
TERMINATED = "Terminated"
# The instance belongs to no group: its metadata answers that it has no target lifecycle state.
NO_STATE = ""
CHAT = "/v1/chat/completions"


class Clock:
    """Seconds, moved only by the test."""

    def __init__(self) -> None:
        self.now = float(NOW)
        self._timers: list[tuple[float, asyncio.Future[None]]] = []

    def time(self) -> float:
        return self.now

    def timer(self, seconds: float) -> asyncio.Future[None]:
        """Done once the clock has moved by `seconds`."""
        timer = asyncio.get_running_loop().create_future()
        self._timers.append((self.now + seconds, timer))
        return timer

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for at, timer in self._timers:
            if at <= self.now and not timer.done():
                timer.set_result(None)
        await asyncio.sleep(0)

    def stop(self) -> None:
        """Whoever still waits on the clock is woken with a cancellation."""
        for _, timer in self._timers:
            timer.cancel()


class World(Host):
    """An instance prepared with the release of `VALUE`, and everything around its agent. Made
    inside the test's event loop. `calls` holds the steps of the transitions in the order they
    happened, and each completion the engine answered; `completions` what reached Auto Scaling,
    and `console` the lines written on the console. `target` is the target lifecycle state in the
    metadata: `NO_STATE` when the metadata has none, None while the metadata does not answer."""

    def __init__(self, folder: Path, target: str | None) -> None:
        super().__init__(ecr=object())
        self.loop = asyncio.get_running_loop()
        self.clock = Clock()
        self.target = target
        self.held = folder / "release.json"
        self.held.write_text(json.dumps(VALUE))
        self.calls: list[str] = []
        self.completions: list[dict[str, str]] = []
        self.console: list[str] = []
        self.items: list[dict[str, Any]] = []
        # When the target state was read, and how many requests the agent sent the engine.
        self.readings: list[float] = []
        self.asked = 0
        # How often a transition went to wait.
        self.waits = 0
        # The engine: nothing listens until the host module starts its unit. While the gate is
        # closed, a request the engine runs is not answered: it stays in flight.
        self.app: Starlette | None = None
        # Whether a launch is on record on the host, and its steps as passed.
        self.launching = False
        self.passed = False
        self.gate = asyncio.Event()
        self.gate.set()
        self.closed = False

    def serves_already(self) -> None:
        """The instance was launched by an agent that ran before this one: its engine was started, and runs."""
        self.app = self.mock()

    def earlier_launch(self) -> launch.Launch:
        """The launch of an agent that ran before the program, on the same instance."""
        return launch.Launch(
            NAME,
            metadata=launch.Metadata(self.fetch),
            autoscaling=self,
            ssm=self,
            secrets=self,
            dynamodb=self,
            host=self,
            engine=launch.Engine(self.fetch),
            held=self.held,
            clock=self.clock.time,
            sleep=self.wait,
            wall=self.clock.time,
        )

    def mock(self) -> Starlette:
        async def pace(seconds: float) -> None:
            await self.gate.wait()

        return create_app(MockSettings(secret=SECRET), clock=self.clock.time, sleep=pace)

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncGenerator[program.Agent]:
        """Run the program beside the test, built as `main` builds it but for the stand-ins."""
        agent = program.Agent(
            program.Environment.read(ENVIRON),
            autoscaling=self,
            ssm=self,
            secrets=self,
            dynamodb=self,
            host=self,
            fetch=self.fetch,
            held=self.held,
            registry=CollectorRegistry(),
            port=0,
            clock=self.clock.time,
            wall=self.clock.time,
            sleep=self.sleep,
            wait=self.wait,
        )
        task = asyncio.ensure_future(agent.run())
        try:
            yield agent
        finally:
            self.closed = True
            self.gate.set()
            task.cancel()
            # A program that ended by itself raises here what ended it.
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.clock.stop()

    @property
    def beats(self) -> list[InstanceHeartbeat]:
        """The rows the serving duties wrote. A launch writes rows too, under its stages' names."""
        rows = [InstanceHeartbeat.from_item(item) for item in self.items]
        return [row for row in rows if row.stage == serving.STAGE]

    async def ticks(self, count: int) -> None:
        """Let `count` ticks of the program pass, each with its reading of the target state."""
        for _ in range(count):
            await self.clock.advance(5)
            await until(lambda: self.readings[-1] == self.clock.now)

    # The clock, as the program waits on it: in the event loop, and from a transition's thread.

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        await self.clock.timer(seconds)

    def wait(self, seconds: float) -> None:
        asyncio.run_coroutine_threadsafe(self._wait(seconds), self.loop).result(timeout=10)

    async def _wait(self, seconds: float) -> None:
        if self.closed:
            raise asyncio.CancelledError
        self.waits += 1
        await self.clock.timer(seconds)

    # The host module.

    def holds(self, image: str, timeout: float | None = None) -> bool:
        return self.app is not None

    def pull(self, image: str, timeout: float | None = None) -> None:
        self.calls.append("pull")

    def start_engine(self, timeout: float | None = None) -> None:
        self.calls.append("start")
        self.app = self.mock()

    def restart_engine(self) -> None:
        self.calls.append("restart")

    def engine_started(self) -> bool:
        return self.app is not None

    def engine_arguments(self) -> tuple[str, ...] | None:
        return None if self.app is None else tuple(VALUE["engine_flags"])

    def begin_launch(self) -> None:
        self.launching = True

    def pass_launch(self) -> None:
        self.passed = True

    def end_launch(self) -> None:
        self.launching = self.passed = False

    def launch_begun(self) -> bool:
        return self.launching

    def launch_passed(self) -> bool:
        return self.passed

    def stop_engine(self) -> None:
        self.calls.append("stop")
        self.app = None

    def write_console(self, line: str, timeout: float | None = None) -> None:
        self.calls.append("console")
        self.console.append(line)

    # The SDK's clients, and the heartbeats table.

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        assert request == {"Name": NAME}
        return {"Parameter": {"Name": NAME, "Value": json.dumps(VALUE)}}

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        assert request == {"SecretId": VALUE["settings"]["secret_id"]}
        return {"SecretString": SECRET}

    def complete_lifecycle_action(self, **request: str) -> dict[str, Any]:
        self.calls.append(request["LifecycleHookName"] + " hook")
        self.completions.append(request)
        return {}

    def Table(self, name: str) -> World:
        assert name == VALUE["settings"]["heartbeat_table"]
        return self

    def put_item(self, Item: dict[str, Any]) -> None:
        self.items.append(Item)

    # The instance metadata and the engine: what answers when the agent opens a URL. The agent
    # calls from a worker thread.

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        answer = asyncio.run_coroutine_threadsafe(self._answer(request, timeout), self.loop)
        return answer.result(timeout=10)  # a guard, not the clock under test

    async def _answer(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        url = request.full_url
        if self.closed:
            return None
        if url == f"{launch.METADATA_URL}/api/token":
            return 200, b"the-session-token"
        if url == f"{launch.METADATA_URL}/meta-data/instance-id":
            return 200, INSTANCE.encode()
        if url == f"{launch.METADATA_URL}/meta-data/autoscaling/target-lifecycle-state":
            self.readings.append(self.clock.now)
            if self.target is None:
                return None
            return (404, b"") if self.target == NO_STATE else (200, self.target.encode())
        assert url.startswith(launch.ENGINE_URL)
        self.asked += 1
        if self.app is None:
            return None
        path = url.removeprefix(launch.ENGINE_URL)
        content = request.data if isinstance(request.data, bytes) else None
        call = asyncio.ensure_future(
            self.ask(request.get_method(), path, headers=dict(request.header_items()), content=content)
        )
        limit = self.clock.timer(timeout) if timeout is not None else None
        await asyncio.wait({call} if limit is None else {call, limit}, return_when=asyncio.FIRST_COMPLETED)
        if not call.done():
            call.cancel()  # the time limit has passed: the agent hangs up
            return None
        if limit is not None:
            limit.cancel()
        answer = call.result()
        if path == "/health":
            self.calls.append("health")
        elif path == CHAT and answer.status_code == 200:
            self.calls.append("completion")
        return answer.status_code, answer.content

    async def ask(self, method: str, path: str, **request: Any) -> httpx.Response:
        """One request to the engine, from the instance itself."""
        assert self.app is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=launch.ENGINE_URL) as http:
            return await http.request(method, path, **request)

    async def student(self) -> asyncio.Future[httpx.Response]:
        """A student's request, in flight on the engine until the gate opens."""
        self.gate.clear()
        body = {"model": "coursework", "messages": [{"role": "user", "content": "Hello"}]}
        sent = asyncio.ensure_future(self.ask("POST", CHAT, headers={"Authorization": f"Bearer {SECRET}"}, json=body))
        names = metric_names(MOCK)
        running = f"{names[Quantity.RUNNING_REQUESTS]} 1.0"
        while running not in (await self.ask("GET", "/metrics")).text.splitlines():
            await asyncio.sleep(0)
        return sent


@pytest.fixture(autouse=True)
def recorded_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The release check stays the real one, and is written down among the steps."""
    real = release.check

    def check(ssm: Any, name: str, path: Path) -> release.Verdict:
        ssm.calls.append("check")
        return real(ssm, name, path)

    monkeypatch.setattr(release, "check", check)


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    # Bounded, so that a program that stalls fails its test and does not stall the run.
    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


async def until(condition: Callable[[], bool]) -> None:
    """Wait for what a worker thread or another task does. A guard, not the clock under test."""

    async def polled() -> None:
        while not condition():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(polled(), timeout=10)


def completion(hook: str) -> dict[str, str]:
    """The call that completes a hook the settings name, for this instance, so that it goes on."""
    return {
        "AutoScalingGroupName": "rhi-coursework",
        "LifecycleHookName": hook,
        "InstanceId": INSTANCE,
        "LifecycleActionResult": "CONTINUE",
    }


def test_a_launch_serves_then_drains_once_the_instance_is_terminated(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = World(tmp_path, IN_SERVICE)  # a new instance: no engine runs, and its disk has no image
        async with world.running():
            # The transition, with its warm-up request, then the duties: a first heartbeat, and a
            # first completion that the engine answers.
            await until(lambda: world.calls.count("completion") == 2 and len(world.beats) == 1)
            assert world.calls[:6] == ["pull", "check", "start", "health", "completion", "launch hook"]
            assert world.completions == [completion("launch")]

            # The group terminates the instance while a student's request runs on it.
            student = await world.student()
            world.target = TERMINATED
            for beats in (2, 3):
                await world.clock.advance(5)
                await until(lambda: len(world.beats) == beats and world.waits >= beats - 1)  # noqa: B023 - called before the next turn
            # The termination handling waits for the engine, and the duties go on beside it.
            assert [beat.running for beat in world.beats] == [0, 1, 1]
            assert world.completions == [completion("launch")]
            assert "stop" not in world.calls

            # The request ends: the termination hook is completed, and the engine stopped.
            world.gate.set()
            assert (await student).status_code == 200
            await world.clock.advance(POLL_SECONDS)
            await until(lambda: world.calls[-1] == "stop")
            assert world.calls[-2:] == ["termination hook", "stop"]
            assert world.completions[1:] == [completion("termination")]

            # The duties are stopped: over the next minute, a heartbeat's 5 seconds twelve times
            # and the minute of a completion, the engine is asked nothing.
            await until(lambda: world.readings[-1] == world.clock.now)
            asked = world.asked
            await world.ticks(12)
            assert world.asked == asked
            assert len(world.beats) == 3
            assert world.completions[1:] == [completion("termination")]

    run(scenario)


def test_the_target_state_is_read_every_5_seconds_and_the_launch_is_run_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = World(tmp_path, None)  # the instance metadata does not answer yet
        async with world.running() as agent:
            await until(lambda: world.readings == [NOW])
            assert agent.server is not None
            assert agent.server.server_address[0] == "127.0.0.1"
            here = f"http://127.0.0.1:{agent.server.server_port}{metrics.ROUTE}"
            scrape = await asyncio.to_thread(launch.fetch, urllib.request.Request(here), 5)
            assert scrape is not None  # the metrics endpoint is up before anything else is done
            assert scrape[0] == 200

            # A reading that failed is followed by the next, and that one says the instance launches.
            world.target = IN_SERVICE
            await world.clock.advance(5)
            await until(lambda: "launch hook" in world.calls)
            await world.ticks(13)

        # The target state was read every 5 seconds, and the launch was run once.
        assert sorted(set(world.readings)) == [NOW + 5 * tick for tick in range(15)]
        assert world.calls[:6] == ["pull", "check", "start", "health", "completion", "launch hook"]
        assert world.calls.count("pull") == 1
        assert world.completions == [completion("launch")]

    run(scenario)


class AnotherRelease(World):
    """An instance whose pool has moved to another release since the instance was prepared."""

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        value = VALUE | {"engine_flags": ["--max-model-len", "32768"]}
        return {"Parameter": {"Name": NAME, "Value": json.dumps(value)}}


def test_an_abandoned_launch_is_not_run_again_and_the_instance_is_left_alone(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = AnotherRelease(tmp_path, IN_SERVICE)
        async with world.running():
            await until(lambda: world.completions != [])
            await world.ticks(3)
            # The group terminates the instance it was told to abandon: no engine runs there to drain.
            world.target = TERMINATED
            await world.ticks(3)

        assert world.calls == ["pull", "check", "launch hook"]
        assert world.completions == [completion("launch") | {"LifecycleActionResult": "ABANDON"}]
        assert world.asked == 0
        assert world.beats == []

    run(scenario)


def test_a_restarted_agent_resumes_serving(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = World(tmp_path, IN_SERVICE)
        world.serves_already()
        async with world.running():
            await until(lambda: len(world.beats) == 1 and world.calls == ["completion"])
            await world.ticks(1)
            await until(lambda: len(world.beats) == 2)

        first, second = world.beats
        assert (first.instance, first.pool, first.written_at) == (INSTANCE, "coursework", NOW)
        assert second.written_at == NOW + 5
        # The completion is the duties' check of the engine. No step of a launch was run: no pull,
        # no check of the release, no start of the engine, and no hook completed.
        assert world.calls == ["completion"]
        assert world.completions == []

    run(scenario)


def test_a_restarted_agent_hands_an_instance_that_left_service_to_the_termination_handling(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = World(tmp_path, TERMINATED)
        world.serves_already()
        async with world.running():
            await until(lambda: world.calls[-1:] == ["stop"])
            await world.ticks(3)

        # The engine was idle: the termination hook was completed at once. Nothing of a launch was
        # run, and no serving duty.
        assert world.calls == ["termination hook", "stop"]
        assert world.completions == [completion("termination")]
        assert world.beats == []

    run(scenario)


class AgentStopped(Exception):
    """The agent that ran before the program stopped where it was."""


class StopsInWarmUp(World):
    """An instance whose agent stops while its launch waits for the warm-up request's answer,
    until `stops` is cleared."""

    stops = True

    async def _answer(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        if self.stops and request.full_url == f"{launch.ENGINE_URL}{CHAT}":
            raise AgentStopped
        return await super()._answer(request, timeout)


def test_a_restarted_agent_runs_an_interrupted_launch_again_before_it_serves(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = StopsInWarmUp(tmp_path, IN_SERVICE)
        with pytest.raises(AgentStopped):
            await asyncio.to_thread(world.earlier_launch().run)
        # The engine's unit is started, and the launch hook still waits.
        assert world.calls == ["pull", "check", "start", "health"]
        assert world.completions == []

        world.stops = False
        async with world.running():
            await until(lambda: len(world.beats) == 1)
            await world.ticks(2)

        # The launch again, by its steps, with the image on disk by now; the hook is completed once.
        assert world.calls[4:9] == ["check", "start", "health", "completion", "launch hook"]
        assert world.completions == [completion("launch")]
        # No row said the instance serves before Auto Scaling took that completion: the rows of
        # the two runs name a launch's stages, up to the hook.
        stages = [InstanceHeartbeat.from_item(item).stage for item in world.items]
        before = stages[: stages.index(serving.STAGE)]
        assert before[-1] == "hook"
        assert len(before) == 5 + 6

    run(scenario)


class RecordStays(World):
    """An instance whose host removes no record of a launch. Auto Scaling takes one completion of
    the launch hook: after it no launch hook waits, and another completion is `refused`, once the
    `throttled` ones before it have failed without saying so."""

    refused = 0
    throttled = 0

    def end_launch(self) -> None:
        raise HostError("record", 1)

    def complete_lifecycle_action(self, **request: str) -> dict[str, Any]:
        if request["LifecycleHookName"] == "launch" and self.completions:
            if self.throttled:
                self.throttled -= 1
                raise ClientError({"Error": {"Code": "Throttling"}}, "CompleteLifecycleAction")
            self.refused += 1
            raise ClientError({"Error": {"Code": launch.NO_ACTION}}, "CompleteLifecycleAction")
        return super().complete_lifecycle_action(**request)

    async def served(self) -> None:
        """The launch of an agent that ran before the program let the instance into service."""
        assert await asyncio.to_thread(self.earlier_launch().run) is launch.Outcome.CONTINUED
        assert self.launching
        assert self.passed
        self.readings.clear()

    async def asks_again(self, seconds: float) -> None:
        """Move the clock, then wait until the termination handling has asked the engine and gone
        to wait: a wait that starts after the clock has moved is ended only by its next move."""
        waits = self.waits
        await self.clock.advance(seconds)
        await until(lambda: self.waits > waits)

    async def drains(self, student: asyncio.Future[httpx.Response]) -> None:
        """The program, with the target state `Terminated`, waits for the student's request, then
        completes the termination hook and stops the engine."""
        # A reading of the target state, then two more questions to the engine.
        await self.asks_again(5)
        await self.asks_again(POLL_SECONDS)
        await self.asks_again(POLL_SECONDS)
        assert self.completions == [completion("launch")]
        assert "stop" not in self.calls

        self.gate.set()
        assert (await student).status_code == 200
        await self.clock.advance(POLL_SECONDS)
        await until(lambda: self.calls[-1] == "stop")
        assert self.calls[-2:] == ["termination hook", "stop"]
        assert self.completions == [completion("launch"), completion("termination")]


def test_an_agent_restarted_on_a_terminated_instance_drains_it_though_its_launch_is_still_on_record(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        world = RecordStays(tmp_path, IN_SERVICE)
        await world.served()
        # The group terminates the instance while a student's request runs on it, and the agent's
        # unit is restarted.
        student = await world.student()
        world.target = TERMINATED
        async with world.running():
            await until(lambda: world.readings != [])
            await world.drains(student)

    run(scenario)


def test_an_agent_restarted_with_its_launch_still_on_record_serves_nothing_on_a_refused_completion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        world = RecordStays(tmp_path, IN_SERVICE)
        await world.served()
        launched, rows = len(world.calls), len(world.items)
        world.throttled = 1
        async with world.running():
            # The completion is sent again. A call that fails says nothing of the hook: no duty
            # runs yet, and the next reading sends it once more.
            await until(lambda: world.throttled == 0)
            assert (world.refused, world.beats) == (0, [])
            # Auto Scaling answers that no launch hook waits, as it would for a hook the settings
            # misname. The agent cannot tell, so it does not say the instance serves, and does not
            # send the completion a third time.
            await world.ticks(1)
            await until(lambda: world.refused == 1)
            await world.ticks(2)
            assert world.refused == 1
            # No step of the launch was run again but the check of the release, and no row was
            # written: none of a launch's stage, and none of the serving duties.
            assert world.calls[launched:] == ["check", "check"]
            assert len(world.items) == rows

            # The instance did serve, and holds a request when its group terminates it.
            student = await world.student()
            world.target = TERMINATED
            await world.drains(student)

    run(scenario)


class StopsAtCompletion(World):
    """An instance whose agent stops as it sends its launch's completion, which does not reach
    Auto Scaling, until `stops` is cleared. With `misnamed`, the pool's parameter then names a
    launch hook the group does not have, and Auto Scaling answers a completion under that name as
    it answers one for which no lifecycle action waits."""

    stops = True
    misnamed = False
    refused = 0

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        answer = super().get_parameter(**request)
        if self.misnamed:
            settings = VALUE["settings"] | {"launch_hook": "another"}
            answer["Parameter"]["Value"] = json.dumps(VALUE | {"settings": settings})
        return answer

    def complete_lifecycle_action(self, **request: str) -> dict[str, Any]:
        if self.stops:
            raise AgentStopped
        if request["LifecycleHookName"] == "another":
            self.refused += 1
            raise ClientError({"Error": {"Code": launch.NO_ACTION}}, "CompleteLifecycleAction")
        return super().complete_lifecycle_action(**request)

    async def interrupted(self) -> None:
        """The launch of an agent that ran before the program passed its steps, and stopped there."""
        with pytest.raises(AgentStopped):
            await asyncio.to_thread(self.earlier_launch().run)
        assert (self.launching, self.passed, self.completions) == (True, True, [])
        self.stops = False


def test_a_restarted_agent_sends_a_lost_completion_again_and_serves_once_auto_scaling_takes_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = StopsAtCompletion(tmp_path, IN_SERVICE)
        await world.interrupted()
        launched, rows = len(world.calls), len(world.items)
        async with world.running():
            # The duties start: a first heartbeat, and a first completion that the engine answers.
            await until(lambda: len(world.beats) == 1 and world.calls.count("completion") == 2)

        # The release is checked and the hook completed. No other step of the launch is run again,
        # and the one row since is the serving duties'.
        assert world.calls[launched:] == ["check", "launch hook", "completion"]
        assert world.completions == [completion("launch")]
        assert len(world.items) == rows + 1
        assert not world.launching

    run(scenario)


def test_a_launch_hook_that_still_waits_under_another_name_lets_no_restarted_agent_serve(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = StopsAtCompletion(tmp_path, IN_SERVICE)
        await world.interrupted()
        world.misnamed = True
        launched, rows = len(world.calls), len(world.items)
        async with world.running():
            await until(lambda: world.refused == 1)
            await world.ticks(3)

        # Auto Scaling took no completion, and the launch hook waits: nothing says the instance
        # serves, and the completion is not sent again. The hook's timeout abandons the instance.
        assert world.refused == 1
        assert world.calls[launched:] == ["check"]
        assert world.completions == []
        assert len(world.items) == rows
        assert world.launching

    run(scenario)


def test_a_restarted_agent_drains_an_instance_whose_abandoned_launch_started_the_engine(tmp_path: Path) -> None:
    async def scenario() -> None:
        # The engine was started, the launch was abandoned at its warm-up request, and the agent's
        # unit was restarted: the launch is on record still, as on an instance that served.
        world = World(tmp_path, TERMINATED)
        world.serves_already()
        world.launching = True
        async with world.running():
            await until(lambda: world.calls[-1:] == ["stop"])
            await world.ticks(3)

        # Nothing runs on the engine: its termination hook is completed at once, and nothing of a
        # launch or of the serving duties is run.
        assert world.calls == ["termination hook", "stop"]
        assert world.completions == [completion("termination")]
        assert world.items == []

    run(scenario)


@pytest.mark.parametrize("target", ["Warmed:Stopped", "Standby", "Detached"])
def test_only_the_target_state_terminated_starts_a_drain(target: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        world = World(tmp_path, IN_SERVICE)
        async with world.running():
            await until(lambda: len(world.beats) == 1)

            world.target = target
            # The serving duties go on, a heartbeat at each of the four ticks. A worker thread
            # writes it beside the reading, and the clock moves on only once it is written: a
            # heartbeat that still waits for the engine's answer then would be cut off.
            for beats in (2, 3, 4, 5):
                await world.ticks(1)
                await until(lambda: len(world.beats) == beats)  # noqa: B023 - called before the next turn
            # No termination hook was completed, and the engine runs.
            assert world.completions == [completion("launch")]
            assert "stop" not in world.calls

    run(scenario)


def test_a_restarted_agent_makes_an_interrupted_canarys_run_again(tmp_path: Path) -> None:
    async def scenario() -> None:
        # The canary's run started the engine and went on record, and the agent stopped before
        # it wrote its line.
        world = World(tmp_path, NO_STATE)
        world.serves_already()
        world.launching = True
        async with world.running():
            await until(lambda: world.console != [])
            await world.ticks(2)

        assert world.calls == ["pull", "check", "start", "health", "completion", "console"]
        assert world.console == [f"rhi-canary served image_digest=sha256:{'a' * 64} model_revision=none"]
        assert world.completions == []
        assert world.items == []

    run(scenario)


def test_an_instance_in_no_group_is_handed_to_the_canary_and_does_nothing_of_a_groups_instance(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        world = World(tmp_path, NO_STATE)
        async with world.running():
            await until(lambda: world.console != [])
            asked = world.asked
            await world.ticks(13)

        # The steps of a launch, once, then the line on the console.
        assert world.calls == ["pull", "check", "start", "health", "completion", "console"]
        assert world.console == [f"rhi-canary served image_digest=sha256:{'a' * 64} model_revision=none"]
        # No hook was completed and no row written, by the canary or by a serving duty: over a
        # minute of readings the engine was asked nothing more.
        assert world.completions == []
        assert world.items == []
        assert world.asked == asked

    run(scenario)


def test_a_target_state_that_comes_after_the_canary_is_acted_on_as_on_any_instance(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = World(tmp_path, NO_STATE)
        async with world.running():
            await until(lambda: world.console != [])
            tried = len(world.calls)

            # The instance is a group's after all: its launch into service runs, and it serves.
            world.target = IN_SERVICE
            await world.clock.advance(5)
            await until(lambda: len(world.beats) == 1)

        assert world.calls[tried : tried + 5] == ["check", "start", "health", "completion", "launch hook"]
        assert world.completions == [completion("launch")]
        assert len(world.console) == 1

    run(scenario)


class SlowPull(World):
    """An instance whose pull of the engine image takes ten minutes."""

    def pull(self, image: str, timeout: float | None = None) -> None:
        super().pull(image, timeout)
        self.wait(600)


def test_the_target_state_is_read_every_5_seconds_while_a_launch_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = SlowPull(tmp_path, IN_SERVICE)
        async with world.running():
            await until(lambda: world.waits == 1)  # the launch is in its pull
            await world.ticks(3)
            # The first two readings are the program's and the launch's own. The readings went on
            # beside the launch, and none of them started it again.
            assert world.readings[2:] == [NOW + 5, NOW + 10, NOW + 15]
            assert world.calls == ["pull"]

            # The pull ends: the launch goes on from where it was, into service.
            await world.clock.advance(585)
            await until(lambda: len(world.beats) == 1)

        assert world.calls.count("pull") == 1
        assert world.completions == [completion("launch")]

    run(scenario)


class SlowSecret(World):
    """Secrets Manager takes its time: the pool's secret is `given` when the test says so."""

    def __init__(self, folder: Path, target: str | None) -> None:
        super().__init__(folder, target)
        self.asked_secret = threading.Event()
        self.given = threading.Event()

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        self.asked_secret.set()
        self.given.wait(timeout=10)  # a guard, not the clock under test
        return super().get_secret_value(**request)


def test_a_duty_at_work_is_waited_for_before_the_engine_is_stopped(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = SlowSecret(tmp_path, IN_SERVICE)
        world.serves_already()
        async with world.running():
            try:
                # The check of the engine is at work, waiting for the pool's secret, when the
                # group terminates the instance. The engine is idle: the hook is completed.
                await until(world.asked_secret.is_set)
                world.target = TERMINATED
                await world.clock.advance(5)
                await until(lambda: world.calls == ["termination hook"])

                world.given.set()
                await until(lambda: world.calls[-1] == "stop")
                await world.ticks(2)
            finally:
                world.given.set()

        # The check found the engine still running, so it restarted nothing, and the engine was
        # stopped only after it: a check left behind would have restarted a stopped engine.
        assert world.calls == ["termination hook", "completion", "stop"]

    run(scenario)


def test_the_program_names_a_setting_its_environment_lacks_and_does_nothing_else() -> None:
    # Only what lets the interpreter find the agent: none of the variables of release.env.
    environ = {"PYTHONPATH": os.pathsep.join(sys.path)}

    ended = subprocess.run(
        [sys.executable, "-m", "rhi_lifecycle"], env=environ, capture_output=True, text=True, timeout=60, check=False
    )

    assert ended.returncode != 0
    assert "RHI_RELEASE_PARAMETER" in ended.stderr
    assert "RHI_HEARTBEAT_TABLE" in ended.stderr

    # One variable less than the boot program writes is refused too, by its name.
    with pytest.raises(program.SettingsError, match="RHI_TERMINATION_HOOK"):
        program.Environment.read({name: value for name, value in ENVIRON.items() if name != "RHI_TERMINATION_HOOK"})
    assert program.Environment.read(ENVIRON).settings == release.Settings(**VALUE["settings"])


def test_without_rhi_engine_the_program_takes_the_engine_from_the_instances_tag() -> None:
    def not_asked() -> str | None:
        raise AssertionError("the tag is read only when RHI_ENGINE is missing")

    # What a boot program writes: the release's engine, which the tag does not override.
    assert program.Environment.read(ENVIRON | {"RHI_ENGINE": "vllm"}, engine_tag=not_asked).engine == "vllm"
    # What a boot program older than the setting writes: no RHI_ENGINE, and the tag names the engine.
    assert program.Environment.read(ENVIRON, engine_tag=lambda: "vllm").engine == "vllm"
    # Neither, the instance having no such tag: the mock's names, as before.
    assert program.Environment.read(ENVIRON, engine_tag=lambda: None).engine == MOCK
    with pytest.raises(program.SettingsError, match="RHI_ENGINE"):
        program.Environment.read(ENVIRON, engine_tag=lambda: "an engine with no row")


class TagMetadata:
    """The instance metadata, for the tags: a session token first, then reads that carry it. The
    first `unanswered` reads of a tag get no answer, as when the service times out."""

    def __init__(self, tags: dict[str, str], unanswered: int = 0) -> None:
        self.tags = tags
        self.unanswered = unanswered
        self.reads = 0
        self.waits: list[float] = []

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        path = request.full_url.removeprefix(launch.METADATA_URL)
        if path == "/api/token" and request.get_method() == "PUT":
            return 200, b"a-session-token"
        if request.get_header("X-aws-ec2-metadata-token") != "a-session-token":
            return 401, b""
        self.reads += 1
        if self.reads <= self.unanswered:
            return None
        key = path.removeprefix("/meta-data/tags/instance/")
        return (200, self.tags[key].encode()) if key in self.tags else (404, b"")

    def engine_tag(self) -> str | None:
        return program.engine_tag(launch.Metadata(self.fetch), wait=self.waits.append)


def test_the_engines_tag_is_read_again_while_the_metadata_does_not_answer() -> None:
    # Answered at once: the tag.
    answered = TagMetadata({program.ENGINE_TAG: "vllm"})
    assert (answered.engine_tag(), answered.reads, answered.waits) == ("vllm", 1, [])
    # The metadata answers that the instance has no such tag: the mock's names, at the first answer.
    absent = TagMetadata({})
    assert (absent.engine_tag(), absent.reads, absent.waits) == (None, 1, [])
    assert program.Environment.read(ENVIRON, engine_tag=absent.engine_tag).engine == MOCK

    # Unanswered twice, then answered: the tag, after two waits.
    late = TagMetadata({program.ENGINE_TAG: "vllm"}, unanswered=program.TAG_ATTEMPTS - 1)
    assert program.Environment.read(ENVIRON, engine_tag=late.engine_tag).engine == "vllm"
    assert (late.reads, late.waits) == (program.TAG_ATTEMPTS, [program.TAG_RETRY_SECONDS] * (program.TAG_ATTEMPTS - 1))

    # Never answered: no engine is taken for the mock. The program ends by the tag's name, and the
    # next start of its unit asks again.
    silent = TagMetadata({program.ENGINE_TAG: "vllm"}, unanswered=program.TAG_ATTEMPTS)
    with pytest.raises(program.SettingsError, match=program.ENGINE_TAG):
        program.Environment.read(ENVIRON, engine_tag=silent.engine_tag)
    assert silent.reads == program.TAG_ATTEMPTS
