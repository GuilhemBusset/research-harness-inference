"""The transition out of service (`rhi_lifecycle.termination`, and the hand-over to it in
`rhi_lifecycle.launch`; system design, section 4.3).

The engine is the mock engine, in process: the agent reads its `/metrics` as a loopback client,
and a stream is held in flight by the mock's own `sleep`. The instance metadata, Auto Scaling, the
release parameter, the pool's secret and the host module are stand-ins. The agent's sleep moves the
test's clock, and the event loop the mock runs on turns only when the agent asks something or
sleeps: no test here needs AWS, a socket, a thread or a wait.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from botocore.exceptions import ClientError

from rhi_common.engine_metrics import MOCK, Quantity, metric_names
from rhi_lifecycle import release
from rhi_lifecycle.host import Host
from rhi_lifecycle.launch import ENGINE_URL, METADATA_URL, Engine, Launch, Metadata, Outcome
from rhi_lifecycle.termination import POLL_SECONDS, Termination
from rhi_mock_engine.app import Settings, create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NAME = "/rhi/pools/coursework/release"
IMAGE = f"registry.example.com/rhi/engine@sha256:{'a' * 64}"
# Made-up values, built here: neither is an instance or a credential.
INSTANCE = "-".join(["i", "0" * 17])
SECRET = "-".join(["the", "pool", "secret", "of", "this", "test"])

VALUE: dict[str, Any] = {
    "engine_image": IMAGE,
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

TERMINATED = "Terminated"
WARM = "Warmed:Stopped"
IN_SERVICE = "InService"
# Twice the termination hook's own timeout, `drain_seconds` plus 60 (section 17): the test's clock
# stops there.
LONG = 2 * (300 + 60)
# Not a multiple of the poll: the stream ends between two questions.
ENDS_AT = 61.0

RUNNING = metric_names(MOCK)[Quantity.RUNNING_REQUESTS]
WAITING = metric_names(MOCK)[Quantity.WAITING_REQUESTS]


class StillWaiting(Exception):
    """The agent was still waiting when the test stopped the world."""


class World(Host):
    """An instance in service, and everything around its agent. `calls` holds what the agent did
    to the host, to the release and to Auto Scaling, in order; `completions` what reached Auto
    Scaling, each with the moment it did."""

    def __init__(self, folder: Path) -> None:
        super().__init__(ecr=object())
        self.loop = asyncio.new_event_loop()
        self.now = 0.0
        self.calls: list[str] = []
        self.completions: list[tuple[float, dict[str, str]]] = []
        self.target = TERMINATED
        self.parameter: dict[str, Any] | None = VALUE
        self.completion_fails = False
        self.held = folder / "release.json"
        self.held.write_text(json.dumps(VALUE))
        self.served = folder / "run" / "served"
        self.engine = create_app(Settings(secret=SECRET), clock=lambda: self.now, sleep=self.pace)
        # While the gate is closed, a stream stops after its second token and stays in flight.
        self.gate = asyncio.Event()
        self.gate.set()
        self.paced = 0
        self.sent: asyncio.Task[httpx.Response] | None = None
        self.ends_at: float | None = None

    def termination(self) -> Termination:
        return Termination(
            NAME,
            metadata=Metadata(self.fetch),
            autoscaling=self,
            ssm=self,
            host=self,
            engine=MOCK,
            fetch=self.fetch,
            sleep=self.sleep,
        )

    def launch(self) -> Launch:
        return Launch(
            NAME,
            metadata=Metadata(self.fetch),
            autoscaling=self,
            ssm=self,
            secrets=self,
            host=self,
            engine=Engine(self.fetch),
            termination=self.termination(),
            held=self.held,
            served=self.served,
            clock=lambda: self.now,
            sleep=self.sleep,
        )

    # The engine, and a student's stream on it.

    async def pace(self, seconds: float) -> None:
        self.paced += 1
        if self.paced > 3:  # the wait for the first byte, and two tokens
            await self.gate.wait()

    async def call(self, request: urllib.request.Request) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.engine, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport) as http:
            return await http.request(
                request.get_method(),
                request.full_url,
                headers=dict(request.header_items()),
                content=request.data if isinstance(request.data, bytes) else None,
            )

    def stream(self, ends_at: float | None) -> None:
        """Put a stream in flight on the engine. It ends at `ends_at` on the test's clock, or never."""
        body = {"model": "coursework", "messages": [{"role": "user", "content": "Read notes.txt"}], "stream": True}
        request = urllib.request.Request(
            f"{ENGINE_URL}/v1/chat/completions",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"},
        )
        self.gate.clear()
        self.paced = 0
        self.ends_at = ends_at
        self.sent = self.loop.create_task(self.call(request))

        async def held() -> None:
            while self.paced <= 3:
                await asyncio.sleep(0)

        self.loop.run_until_complete(asyncio.wait_for(held(), timeout=10))

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.sent is not None and self.ends_at is not None and self.now >= self.ends_at and not self.gate.is_set():
            self.gate.set()
            answer = self.loop.run_until_complete(self.sent)
            assert answer.status_code == 200
            assert answer.text.rstrip().endswith("data: [DONE]")
        if self.now >= LONG:
            raise StillWaiting

    def close(self) -> None:
        self.gate.set()
        if self.sent is not None:
            self.loop.run_until_complete(self.sent)
        self.loop.close()

    # The host module.

    def holds(self, image: str, timeout: float | None = None) -> bool:
        return image == IMAGE

    def pull(self, image: str, timeout: float | None = None) -> None:
        self.calls.append("pull")

    def start_engine(self, timeout: float | None = None) -> None:
        self.calls.append("start")

    def stop_engine(self) -> None:
        self.calls.append("stop")

    # The SDK's clients.

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        if request != {"Name": NAME} or self.parameter is None:
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "no"}}, "GetParameter")
        return {"Parameter": {"Name": NAME, "Value": json.dumps(self.parameter)}}

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        return {"SecretString": SECRET}

    def complete_lifecycle_action(self, **request: str) -> dict[str, Any]:
        if self.completion_fails:
            raise ClientError({"Error": {"Code": "ValidationError", "Message": "no"}}, "CompleteLifecycleAction")
        self.calls.append("complete")
        self.completions.append((self.now, request))
        return {}

    # The instance metadata and the engine: what answers when the agent opens a URL.

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        url = request.full_url
        if url == f"{METADATA_URL}/api/token":
            return 200, b"the-session-token"
        if url.startswith(f"{METADATA_URL}/meta-data/"):
            items = {"autoscaling/target-lifecycle-state": self.target, "instance-id": INSTANCE}
            return 200, items[url.removeprefix(f"{METADATA_URL}/meta-data/")].encode()
        answer = self.loop.run_until_complete(self.call(request))
        return answer.status_code, answer.content


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    # The release check stays the real one, and is written down among the steps.
    real = release.check

    def check(ssm: Any, name: str, path: Path) -> release.Verdict:
        ssm.calls.append("check")
        return real(ssm, name, path)

    monkeypatch.setattr(release, "check", check)
    world = World(tmp_path)
    yield world
    world.close()


def completion(hook: str) -> dict[str, str]:
    """The call that completes a hook the settings name, for this instance, so that it goes on."""
    return {
        "AutoScalingGroupName": "rhi-coursework",
        "LifecycleHookName": hook,
        "InstanceId": INSTANCE,
        "LifecycleActionResult": "CONTINUE",
    }


def test_the_hook_is_not_completed_while_a_stream_is_in_flight(world: World) -> None:
    world.stream(ends_at=None)

    with pytest.raises(StillWaiting):
        world.termination().run()

    # Past the hook's own timeout, nothing reached Auto Scaling and the engine was not stopped.
    assert world.now >= LONG
    assert world.calls == []


def test_the_hook_is_completed_within_one_poll_of_the_streams_end(world: World) -> None:
    world.stream(ends_at=ENDS_AT)

    assert world.termination().run() is Outcome.DRAINED

    [(completed_at, request)] = world.completions
    assert request == completion("termination")
    assert ENDS_AT <= completed_at <= ENDS_AT + POLL_SECONDS
    # The engine's unit is stopped only once the hook is completed.
    assert world.calls == ["complete", "stop"]


def test_a_return_to_the_warm_pool_drains(world: World) -> None:
    # The instance is promoted, and serves.
    world.target = IN_SERVICE
    assert world.launch().run() is Outcome.CONTINUED
    assert world.calls == ["check", "start", "complete"]
    assert [request for _, request in world.completions] == [completion("launch")]

    # It returns to the warm pool with a stream in flight: the wait state and the target state of a
    # new instance that enters the pool (evidence E10).
    world.target = WARM
    world.stream(ends_at=ENDS_AT)
    assert world.launch().run() is Outcome.DRAINED

    # No pull and no check of the release, as an entry would do: the stream is waited for, then the
    # termination hook is completed, once.
    assert world.calls[3:] == ["complete", "stop"]
    [(completed_at, request)] = world.completions[1:]
    assert request == completion("termination")
    assert ENDS_AT <= completed_at <= ENDS_AT + POLL_SECONDS


def test_only_an_engine_that_reports_nothing_running_and_nothing_waiting_is_idle(world: World) -> None:
    def idle(answer: tuple[int, bytes] | None) -> bool:
        return Termination(
            NAME, Metadata(), autoscaling=world, ssm=world, host=world, engine=MOCK, fetch=lambda request, limit: answer
        ).idle()

    def report(running: int, waiting: int) -> bytes:
        return f"{RUNNING} {running}\n{WAITING} {waiting}\n".encode()

    assert idle((200, report(0, 0)))
    assert not idle((200, report(0, 1)))
    assert not idle((200, report(1, 0)))
    # An engine that says nothing has not said that it is idle.
    assert not idle(None)
    assert not idle((503, report(0, 0)))
    assert not idle((200, f"{RUNNING} 0\n".encode()))


def test_an_instance_that_is_not_terminated_is_left_alone(world: World) -> None:
    # A warm-pool target is the launch's to read: only the file that says the instance has served
    # tells a return from an entry.
    for target in (IN_SERVICE, WARM):
        world.target = target
        assert world.termination().run() is Outcome.NOT_A_TERMINATION
    assert world.calls == []


def test_what_cannot_be_completed_leaves_the_engine_running(world: World) -> None:
    # The call to Auto Scaling fails: the hook is not completed, so the engine is not stopped.
    world.completion_fails = True
    assert world.termination().run() is Outcome.NOT_COMPLETED

    # Without the parameter no hook is named: the agent does not wait for a completion it cannot make.
    world.parameter = None
    world.stream(ends_at=None)
    assert world.termination().run() is Outcome.NOT_COMPLETED

    assert world.calls == []
    assert world.now == 0
