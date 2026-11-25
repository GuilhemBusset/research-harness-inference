"""The run of a canary (`rhi_lifecycle.canary`; system design, sections 4.3 and 5.1), on one
stand-in for everything around the agent: the instance metadata, which has no lifecycle state, and
the engine, where the agent opens a URL; the host module, which is the container runtime and the
console; the release parameter and the pool's secret, as SDK clients; and a clock that only the
stand-in moves. The same stand-in is Auto Scaling and the heartbeats table, and records every call
they get: a canary makes none. No test here needs AWS, docker or a wait.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

from rhi_lifecycle.canary import Canary, Outcome
from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.launch import ENGINE_URL, METADATA_URL, Engine, Metadata, MetadataError, NoSuchItemError

if TYPE_CHECKING:
    import urllib.request
    from collections.abc import Callable
    from pathlib import Path

NAME = "/rhi/pools/coursework/release"
DIGEST = "sha256:" + "a" * 64
REVISION = "c" * 40
# Made-up values, built here: neither is an instance or a credential.
INSTANCE = "-".join(["i", "0" * 17])
SECRET = "-".join(["the", "pool", "secret", "of", "this", "test"])

VALUE: dict[str, Any] = {
    "engine_image": f"registry.example.com/rhi/engine@{DIGEST}",
    "model_revision": REVISION,
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

SERVED = f"rhi-canary served image_digest={DIGEST} model_revision={REVISION}"


class World(Host):
    """An instance in no group, prepared with the release of `VALUE`, and everything around its
    agent. `calls` holds the steps in the order they happened, `console` the lines written on the
    console, `completions` what reached Auto Scaling and `items` what reached the heartbeats table."""

    def __init__(self, folder: Path) -> None:
        super().__init__(ecr=object())
        self.now = 0.0
        self.calls: list[str] = []
        self.console: list[str] = []
        self.completions: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.parameter: dict[str, Any] | None = copy.deepcopy(VALUE)
        self.held = folder / "release.json"
        self.held.write_text(json.dumps(VALUE))
        # The instance metadata: whether it answers.
        self.metadata_answers = True
        # The host: how a pull, a start and a write on the console go.
        self.pull_fails = False
        self.start_fails = False
        self.console_fails = False
        # The engine: whether it is started, whether its health route then answers 200, and its
        # answer to the warm-up request.
        self.started = False
        self.healthy = True
        # Whether a launch is on record on the host.
        self.on_record = False
        self.warm_up: tuple[int, bytes] = (200, b'{"choices": []}')

    def canary(self) -> Canary:
        return Canary(
            NAME,
            ssm=self,
            secrets=self,
            host=self,
            engine=Engine(self.fetch),
            held=self.held,
            clock=lambda: self.now,
            sleep=self.sleep,
        )

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    # The host module: the container runtime, the engine's unit and the console.

    def pull(self, image: str, timeout: float | None = None) -> None:
        self.calls.append("pull")
        if self.pull_fails or image != VALUE["engine_image"]:
            raise HostError("pull", 1)
        self.now += 120

    def start_engine(self, timeout: float | None = None) -> None:
        self.calls.append("start")
        if self.start_fails:
            raise HostError("start", 1)
        self.started = True

    def begin_launch(self) -> None:
        self.on_record = True

    def write_console(self, line: str, timeout: float | None = None) -> None:
        self.calls.append("console")
        if self.console_fails:
            raise HostError("console", 1)
        self.console.append(line)

    # The SDK's clients: the release parameter and the pool's secret, which a canary reads, and
    # Auto Scaling and the heartbeats table, which it is not to call.

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        if request != {"Name": NAME} or self.parameter is None:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetParameter")
        return {"Parameter": {"Name": NAME, "Value": json.dumps(self.parameter)}}

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        assert request == {"SecretId": VALUE["settings"]["secret_id"]}
        return {"SecretString": SECRET}

    def complete_lifecycle_action(self, **request: Any) -> dict[str, Any]:
        self.completions.append(request)
        return {}

    def Table(self, name: str) -> World:
        return self

    def put_item(self, **request: Any) -> dict[str, Any]:
        self.items.append(request)
        return {}

    # The instance metadata and the engine: what answers when the agent opens a URL.

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        url, method = request.full_url, request.get_method()
        if url.startswith(METADATA_URL):
            if not self.metadata_answers:
                return None
            if url == f"{METADATA_URL}/api/token":
                return 200, b"the-session-token"
            # The instance belongs to no group: the metadata has no lifecycle state to give.
            return (200, INSTANCE.encode()) if url.endswith("/meta-data/instance-id") else (404, b"")
        if not self.started:
            return None  # nothing listens before the engine's start
        if (url, method) == (f"{ENGINE_URL}/health", "GET"):
            self.calls.append("health")
            return (200 if self.healthy else 503), b""
        assert (url, method) == (f"{ENGINE_URL}/v1/chat/completions", "POST")
        self.calls.append("warm-up")
        if request.get_header("Authorization") != f"Bearer {SECRET}":
            return 401, b""
        return self.warm_up


def test_the_metadata_of_an_instance_in_no_group_says_it_has_no_lifecycle_state(tmp_path: Path) -> None:
    world = World(tmp_path)
    metadata = Metadata(world.fetch)

    with pytest.raises(NoSuchItemError):
        metadata.target_state()
    assert metadata.instance_id() == INSTANCE

    # A metadata service that does not answer has not said so: the reading failed.
    world.metadata_answers = False
    with pytest.raises(MetadataError) as raised:
        metadata.target_state()
    assert not isinstance(raised.value, NoSuchItemError)


def test_a_canary_starts_the_engine_as_a_launch_does_and_says_served_once_the_warm_up_is_answered(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)

    assert world.canary().run() is Outcome.SERVED

    assert world.calls == ["pull", "start", "health", "warm-up", "console"]
    assert world.console == [SERVED]
    # The run is on record still: an agent restarted here takes the started engine for no instance
    # that serves, and makes the run again.
    assert world.on_record
    # No hook is completed, and no heartbeat row is written.
    assert world.completions == []
    assert world.items == []


@pytest.mark.parametrize(("measured", "deadline"), [(None, 900), (600, 1200)])
def test_an_engine_that_never_becomes_healthy_fails_in_the_weights_stage_at_the_deadline(
    measured: int | None, deadline: int, tmp_path: Path
) -> None:
    world = World(tmp_path)
    world.healthy = False
    assert world.parameter is not None
    if measured is not None:
        world.parameter["measured_start_seconds"] = measured

    assert world.canary().run() is Outcome.FAILED

    # The launch hook's own time: 15 minutes, or twice the measured start when that is longer.
    assert world.now == deadline
    assert list(dict.fromkeys(world.calls)) == ["pull", "start", "health", "console"]
    assert world.console == ["rhi-canary failed stage=weights"]
    assert world.completions == []
    assert world.items == []


def no_parameter(world: World) -> None:
    world.parameter = None


def failed_pull(world: World) -> None:
    world.pull_fails = True


def another_release(world: World) -> None:
    assert world.parameter is not None
    world.parameter["engine_flags"] = ["--max-model-len", "32768"]


def failed_start(world: World) -> None:
    world.start_fails = True


def failed_warm_up(world: World) -> None:
    world.warm_up = (500, b"")


@pytest.mark.parametrize(
    ("failure", "stage"),
    [
        (no_parameter, "image"),
        (failed_pull, "image"),
        (another_release, "release-check"),
        (failed_start, "container-start"),
        (failed_warm_up, "warm-up"),
    ],
)
def test_a_canary_that_fails_names_the_last_stage_it_reached(
    failure: Callable[[World], None], stage: str, tmp_path: Path
) -> None:
    world = World(tmp_path)
    failure(world)

    assert world.canary().run() is Outcome.FAILED

    assert world.console == [f"rhi-canary failed stage={stage}"]
    assert world.completions == []
    assert world.items == []


def test_a_line_that_cannot_be_written_does_not_end_the_agent(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.console_fails = True

    assert world.canary().run() is Outcome.SERVED

    assert world.calls[-1] == "console"
    assert world.console == []
