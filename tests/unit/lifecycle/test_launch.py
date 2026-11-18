"""The launch transitions (`rhi_lifecycle.launch`; system design, section 4.3), on one stand-in
for everything around the agent: the instance metadata and the engine, where the agent opens a
URL; Auto Scaling, the release parameter and the pool's secret, as SDK clients; the host module;
and a clock that only the stand-in moves. No test here needs AWS, docker or a wait, and the one
socket is a server of the test's own, on the loopback interface."""

from __future__ import annotations

import asyncio
import copy
import http.server
import json
import threading
import urllib.request
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from botocore.exceptions import ClientError

from rhi_common.engine_metrics import MOCK
from rhi_lifecycle import release
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.launch import ENGINE_URL, METADATA_URL, Engine, Launch, Metadata, Outcome, fetch
from rhi_lifecycle.termination import Termination
from rhi_mock_engine.app import Settings, create_app

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NAME = "/rhi/pools/coursework/release"
IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/rhi/engine@sha256:{'a' * 64}"
# Made-up values, built here: neither is an instance or a credential.
INSTANCE = "-".join(["i", "0" * 17])
SECRET = "-".join(["the", "pool", "secret", "of", "this", "test"])
TOKEN = b"the-session-token"

VALUE: dict[str, Any] = {
    "engine_image": IMAGE,
    "model_revision": None,
    "engine_flags": ["--max-model-len", "65536"],
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

WARM = "Warmed:Stopped"
IN_SERVICE = "InService"
# A hook with no timeout of its own waits this long (evidence E10): the test's clock stops there.
AN_HOUR = 3600


class StillWaiting(Exception):
    """The agent was still waiting when the test stopped the world."""


def refusal(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, operation)


class World(Host):
    """An instance prepared with the release of `VALUE`, and everything around its agent. `calls`
    holds the steps in the order they happened; `completions` what reached Auto Scaling."""

    def __init__(self, folder: Path, target: str, *, on_disk: bool, measured: int | None = None) -> None:
        super().__init__(ecr=object())
        self.now = 0.0
        self.calls: list[str] = []
        self.completions: list[dict[str, str]] = []
        self.target: str | None = target
        self.parameter: dict[str, Any] | None = copy.deepcopy(VALUE)
        if self.parameter is not None and measured is not None:
            self.parameter["measured_start_seconds"] = measured
        self.held = folder / "release.json"
        self.held.write_text(json.dumps(VALUE))
        self.served = folder / "run" / "served"
        # The host: whether its docker holds the image, and how a pull and a start go.
        self.on_disk = on_disk
        self.pull_seconds = 120.0
        self.pull_fails = False
        self.pull_limits: list[float | None] = []
        self.start_fails = False
        # The engine: healthy this long after its start (None: never), and its answer to the
        # warm-up request (None: none, the request stays unanswered).
        self.started_at: float | None = None
        self.start_seconds: float | None = 0.0
        self.warm_up: tuple[int, bytes] | None = (200, b'{"choices": []}')

    def launch(self) -> Launch:
        metadata = Metadata(self.fetch)
        return Launch(
            NAME,
            metadata=metadata,
            autoscaling=self,
            ssm=self,
            secrets=self,
            host=self,
            engine=Engine(self.fetch),
            # An instance that leaves service is handed over: tests/unit/lifecycle/test_termination.py.
            termination=Termination(
                NAME, metadata, autoscaling=self, ssm=self, host=self, engine=MOCK, fetch=self.fetch, sleep=self.sleep
            ),
            held=self.held,
            served=self.served,
            clock=lambda: self.now,
            sleep=self.sleep,
        )

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now >= AN_HOUR:
            raise StillWaiting

    # The host module.

    def holds(self, image: str, timeout: float | None = None) -> bool:
        return image == IMAGE and self.on_disk

    def pull(self, image: str, timeout: float | None = None) -> None:
        self.calls.append("pull")
        self.pull_limits.append(timeout)
        if self.pull_fails or image != IMAGE:
            raise HostError("pull", 1)
        if timeout is not None and self.pull_seconds > timeout:
            # The host module stops a pull that is still running at its time limit.
            self.now += timeout
            raise HostError("pull")
        self.now += self.pull_seconds
        self.on_disk = True

    def start_engine(self, timeout: float | None = None) -> None:
        self.calls.append("start")
        if self.start_fails:
            raise HostError("start", 1)
        self.started_at = self.now

    # The SDK's clients.

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        if request != {"Name": NAME} or self.parameter is None:
            raise refusal("GetParameter")
        return {"Parameter": {"Name": NAME, "Value": json.dumps(self.parameter)}}

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        if request != {"SecretId": VALUE["settings"]["secret_id"]}:
            raise refusal("GetSecretValue")
        return {"SecretString": SECRET}

    def complete_lifecycle_action(self, **request: str) -> dict[str, Any]:
        self.calls.append("complete")
        self.completions.append(request)
        return {}

    # The instance metadata and the engine: what answers when the agent opens a URL.

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        url, method = request.full_url, request.get_method()
        if url == f"{METADATA_URL}/api/token":
            asked = method == "PUT" and request.get_header("X-aws-ec2-metadata-token-ttl-seconds")
            return (200, TOKEN) if asked else (400, b"")
        if url.startswith(f"{METADATA_URL}/meta-data/"):
            # IMDSv2: no item without the session token.
            if request.get_header("X-aws-ec2-metadata-token") != TOKEN.decode():
                return 401, b""
            items = {"autoscaling/target-lifecycle-state": self.target, "instance-id": INSTANCE}
            item = items.get(url.removeprefix(f"{METADATA_URL}/meta-data/"))
            return (404, b"") if item is None else (200, item.encode())
        if self.started_at is None:
            return None  # nothing listens before the engine's start
        if (url, method) == (f"{ENGINE_URL}/health", "GET"):
            self.calls.append("health")
            healthy = self.start_seconds is not None and self.now - self.started_at >= self.start_seconds
            return (200 if healthy else 503), b""
        if (url, method) == (f"{ENGINE_URL}/v1/chat/completions", "POST"):
            self.calls.append("warm-up")
            if self.warm_up is None:
                raise StillWaiting
            if request.get_header("Authorization") != f"Bearer {SECRET}":
                return 401, b""
            assert isinstance(request.data, bytes)
            body = json.loads(request.data)
            if body.get("model") != VALUE["settings"]["pool"] or not body.get("messages"):
                return 400, b""
            return self.warm_up
        return 404, b""


@pytest.fixture(autouse=True)
def recorded_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The release check stays the real one, and is written down among the steps."""
    real = release.check

    def check(ssm: Any, name: str, path: Path) -> release.Verdict:
        ssm.calls.append("check")
        return real(ssm, name, path)

    monkeypatch.setattr(release, "check", check)


def completion(result: str) -> dict[str, str]:
    """The call that completes the launch hook the settings name, for this instance."""
    return {
        "AutoScalingGroupName": "rhi-coursework",
        "LifecycleHookName": "launch",
        "InstanceId": INSTANCE,
        "LifecycleActionResult": result,
    }


def test_an_entry_to_the_warm_pool_pulls_the_image_checks_the_release_and_completes(tmp_path: Path) -> None:
    world = World(tmp_path, WARM, on_disk=False)

    assert world.launch().run() is Outcome.CONTINUED

    assert world.calls == ["pull", "check", "complete"]
    assert world.completions == [completion("CONTINUE")]
    assert world.started_at is None  # the group stops the instance: no engine is started


def test_a_promotion_checks_starts_waits_warms_up_and_only_then_completes(tmp_path: Path) -> None:
    world = World(tmp_path, IN_SERVICE, on_disk=True)
    world.start_seconds = 4.0  # not healthy at once: the agent asks again

    assert world.launch().run() is Outcome.CONTINUED

    assert world.calls == ["check", "start", "health", "health", "health", "warm-up", "complete"]
    assert world.completions == [completion("CONTINUE")]


def test_a_promotion_is_not_completed_while_the_warm_up_request_is_unanswered(tmp_path: Path) -> None:
    world = World(tmp_path, IN_SERVICE, on_disk=True)
    world.warm_up = None

    with pytest.raises(StillWaiting):
        world.launch().run()

    assert world.calls == ["check", "start", "health", "warm-up"]
    assert world.completions == []


def test_a_launch_straight_into_service(tmp_path: Path) -> None:
    # The warm pool never held this instance: its disk has no image. The pull alone outlasts twice
    # the measured start, and only the hook's own timeout bounds this launch.
    world = World(tmp_path, IN_SERVICE, on_disk=False, measured=200)
    world.pull_seconds = 500.0
    world.start_seconds = 100.0

    assert world.launch().run() is Outcome.CONTINUED

    steps = [step for step in world.calls if step != "health"]
    assert steps == ["pull", "check", "start", "warm-up", "complete"]
    assert world.pull_limits == [None]
    assert world.completions == [completion("CONTINUE")]
    assert world.now >= 600


def another_release(world: World) -> None:
    assert world.parameter is not None
    world.parameter["engine_flags"] = ["--max-model-len", "32768"]


def failed_pull(world: World) -> None:
    world.pull_fails = True


def failed_start(world: World) -> None:
    world.start_fails = True


def never_healthy(world: World) -> None:
    world.start_seconds = None


def failed_warm_up(world: World) -> None:
    world.warm_up = (500, b"")


@pytest.mark.parametrize(
    ("target", "on_disk", "failure", "steps"),
    [
        pytest.param(WARM, False, another_release, ["pull", "check"], id="entry-release-differs"),
        pytest.param(IN_SERVICE, True, another_release, ["check"], id="promotion-release-differs"),
        pytest.param(WARM, False, failed_pull, ["pull"], id="entry-pull-fails"),
        pytest.param(IN_SERVICE, False, failed_pull, ["pull"], id="straight-launch-pull-fails"),
        pytest.param(IN_SERVICE, True, failed_start, ["check", "start"], id="engine-does-not-start"),
        pytest.param(IN_SERVICE, True, never_healthy, ["check", "start", "health"], id="engine-never-healthy"),
        pytest.param(
            IN_SERVICE, True, failed_warm_up, ["check", "start", "health", "warm-up"], id="warm-up-request-fails"
        ),
    ],
)
def test_failures_abandon(
    target: str, on_disk: bool, failure: Callable[[World], None], steps: list[str], tmp_path: Path
) -> None:
    world = World(tmp_path, target, on_disk=on_disk, measured=200)
    failure(world)

    assert world.launch().run() is Outcome.ABANDONED

    # The steps up to the one that failed, each once but for the health route, then one completion.
    assert list(dict.fromkeys(world.calls)) == [*steps, "complete"]
    assert world.completions == [completion("ABANDON")]
    assert not world.served.exists()


def test_each_transition_keeps_its_own_deadline(tmp_path: Path) -> None:
    def world(name: str, target: str, measured: int | None) -> World:
        folder = tmp_path / name
        folder.mkdir()
        return World(folder, target, on_disk=target == IN_SERVICE, measured=measured)

    # An entry to the warm pool has 15 minutes, whatever the pool's measured start.
    entry = world("entry-in-time", WARM, 200)
    entry.pull_seconds = 899.0
    assert entry.launch().run() is Outcome.CONTINUED

    entry = world("entry-too-long", WARM, 200)
    entry.pull_seconds = 5000.0
    assert entry.launch().run() is Outcome.ABANDONED
    assert entry.now == 900
    assert entry.completions == [completion("ABANDON")]

    # A promotion has twice the measured start: 400 seconds for a start of 200.
    promotion = world("promotion-in-time", IN_SERVICE, 200)
    promotion.start_seconds = 397.0
    assert promotion.launch().run() is Outcome.CONTINUED
    assert 397 <= promotion.now < 400

    promotion = world("promotion-too-long", IN_SERVICE, 200)
    promotion.start_seconds = None
    assert promotion.launch().run() is Outcome.ABANDONED
    assert promotion.now == 400
    assert promotion.completions == [completion("ABANDON")]

    # With no measured start the agent sets no deadline: it still waits when the test stops, far
    # past the 400 seconds and the 15 minutes.
    unmeasured = world("promotion-unmeasured", IN_SERVICE, None)
    unmeasured.start_seconds = None
    with pytest.raises(StillWaiting):
        unmeasured.launch().run()
    assert unmeasured.now >= AN_HOUR
    assert unmeasured.completions == []


@pytest.mark.parametrize("target", ["Terminated", "Warmed:Terminated", None])
def test_nothing_is_done_outside_a_launch(target: str | None, tmp_path: Path) -> None:
    world = World(tmp_path, IN_SERVICE, on_disk=True)
    world.target = target

    # Without a target state the agent does not know its transition.
    assert world.launch().run() is (Outcome.NOT_COMPLETED if target is None else Outcome.NOT_A_LAUNCH)
    assert world.calls == []


def test_without_the_parameter_no_hook_is_named_and_nothing_is_completed(tmp_path: Path) -> None:
    world = World(tmp_path, WARM, on_disk=False)
    world.parameter = None

    assert world.launch().run() is Outcome.NOT_COMPLETED
    assert world.calls == []


def test_the_warm_up_is_a_request_the_engine_contract_answers() -> None:
    # The mock engine, in process, 30 seconds after which it is healthy.
    now = 0.0
    app = create_app(Settings(secret=SECRET, start_seconds=30), clock=lambda: now)

    def to_the_mock(request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        async def call() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
                return await http.request(
                    request.get_method(),
                    request.full_url,
                    headers=dict(request.header_items()),
                    content=request.data if isinstance(request.data, bytes) else None,
                )

        answer = asyncio.run(call())
        return answer.status_code, answer.content

    engine = Engine(to_the_mock)
    assert not engine.healthy()
    now = 30.0
    assert engine.healthy()
    assert engine.warm_up("coursework", SECRET)
    assert not engine.warm_up("coursework", "another secret")


def test_a_request_gives_the_status_and_the_body_or_nothing_when_nothing_answers() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"ready" if self.path == "/health" else b"not ready"
            self.send_response(200 if self.path == "/health" else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    here = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert fetch(urllib.request.Request(f"{here}/health"), 5) == (200, b"ready")
        # A refusal is an answer: its status is what the agent reads.
        answer = fetch(urllib.request.Request(f"{here}/other"), 5)
        assert answer is not None
        assert answer[0] == 503
    finally:
        server.shutdown()
        server.server_close()

    # The port is closed now: nothing listens, as before the engine's start.
    assert fetch(urllib.request.Request(f"{here}/health"), 5) is None
