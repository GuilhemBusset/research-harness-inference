"""Independent launch-transition witnesses for issue 72; all IO is replaced in process."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from rhi_lifecycle.host import Host, HostError
from rhi_lifecycle.launch import ENGINE_URL, METADATA_URL, Engine, Launch, Metadata, Outcome

if TYPE_CHECKING:
    import urllib.request
    from pathlib import Path


class Rig(Host):
    """A disk, metadata server, SDK clients and engine with a controlled clock."""

    def __init__(self, folder: Path, target: str, *, cached: bool = True, measured: int | None = 200) -> None:
        super().__init__(ecr=object())
        self.target = target
        self.cached = cached
        self.now = 0.0
        self.events: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.reads = 0
        self.pull_time = 0.0
        self.start_time = 0.0
        self.health_time = 0.0
        self.health_ready = True
        self.failure = ""
        self.document: dict[str, Any] = {
            "engine_image": "registry.example.com/engine@sha256:" + "d" * 64,
            "model_revision": None,
            "engine_flags": [],
            "files": {"bucket": "artifacts", "key": "release.tar", "sha256": "e" * 64},
            "measured_start_seconds": measured,
            "settings": {
                "pool": "mock", "group": "mock-group", "launch_hook": "prepare",
                "termination_hook": "leave", "log_group": "mock-logs",
                "workspace_url": "https://metrics.example.com", "heartbeat_table": "beats",
                "secret_id": "mock-engine",
            },
        }
        self.held = folder / "release.json"
        self.held.write_text(json.dumps(self.document))
        self.served = folder / "served"

    def launch(self) -> Launch:
        return Launch(
            "release", Metadata(self.fetch), self, self, self, self, Engine(self.fetch),
            held=self.held, served=self.served, clock=lambda: self.now, sleep=self.sleep,
        )

    def sleep(self, duration: float) -> None:
        self.now += duration

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        assert request == {"Name": "release"}
        self.reads += 1
        if self.reads > 1:
            self.events.append("check")
        return {"Parameter": {"Value": json.dumps(self.document)}}

    def get_secret_value(self, **request: Any) -> dict[str, str]:
        assert request == {"SecretId": "mock-engine"}
        return {"SecretString": "made-up-test-value"}

    def complete_lifecycle_action(self, **request: Any) -> None:
        self.events.append("complete")
        self.results.append(request)

    def holds(self, image: str, timeout: float | None = None) -> bool:
        assert image == self.document["engine_image"]
        return self.cached

    def pull(self, image: str, timeout: float | None = None) -> None:
        assert image == self.document["engine_image"]
        self.events.append("pull")
        if self.failure == "pull":
            raise HostError("pull", 1)
        if timeout is not None and self.pull_time >= timeout:
            self.now += timeout
            raise HostError("pull")
        self.now += self.pull_time

    def start_engine(self, timeout: float | None = None) -> None:
        self.events.append("start")
        self.now += self.start_time
        if self.failure == "start":
            raise HostError("start", 1)

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        url = request.full_url
        if url == f"{METADATA_URL}/api/token":
            assert request.get_method() == "PUT"
            return 200, b"test-session"
        if url.startswith(f"{METADATA_URL}/meta-data/"):
            assert request.get_header("X-aws-ec2-metadata-token") == "test-session"
            if url.endswith("target-lifecycle-state"):
                return 200, self.target.encode()
            return 200, ("i-" + "0" * 17).encode()
        if url == f"{ENGINE_URL}/health":
            self.events.append("health")
            self.now += self.health_time if timeout is None else min(self.health_time, timeout)
            return (200 if self.health_ready else 503), b""
        assert url == f"{ENGINE_URL}/v1/chat/completions"
        self.events.append("warm-up")
        assert self.results == [], "The hook must remain open while inference runs"
        assert request.get_header("Authorization") == "Bearer made-up-test-value"
        assert isinstance(request.data, bytes)
        body = json.loads(request.data)
        assert body["model"] == "mock" and body["messages"]
        if self.failure == "warm-up":
            return 500, b""
        return 200, b'{"choices":[{"message":{"content":"ready"}}]}'


def assert_result(rig: Rig, result: str) -> None:
    assert rig.results == [{
        "AutoScalingGroupName": "mock-group", "LifecycleHookName": "prepare",
        "InstanceId": "i-" + "0" * 17, "LifecycleActionResult": result,
    }]


@pytest.mark.parametrize("target", ["Warmed:Stopped", "Warmed:Running", "Warmed:Hibernated"])
def test_entry_and_served_return_are_distinct(tmp_path: Path, target: str) -> None:
    rig = Rig(tmp_path, target, cached=False)
    assert rig.launch().run() is Outcome.CONTINUED
    assert rig.events == ["pull", "check", "complete"]
    assert_result(rig, "CONTINUE")
    rig.served.touch()
    rig.events.clear()
    assert rig.launch().run() is Outcome.NOT_A_LAUNCH
    assert rig.events == []


@pytest.mark.parametrize("cached", [True, False])
def test_service_waits_for_real_inference(tmp_path: Path, cached: bool) -> None:
    rig = Rig(tmp_path, "InService", cached=cached)
    # A cold launch may outlast the measured promotion deadline.
    rig.pull_time = 450
    assert rig.launch().run() is Outcome.CONTINUED
    prefix = [] if cached else ["pull"]
    assert rig.events == [*prefix, "check", "start", "health", "warm-up", "complete"]
    assert_result(rig, "CONTINUE")


@pytest.mark.parametrize("failure", ["release", "pull", "start", "health", "warm-up"])
def test_failed_steps_abandon(tmp_path: Path, failure: str) -> None:
    rig = Rig(tmp_path, "Warmed:Stopped" if failure == "pull" else "InService")
    rig.failure = failure
    if failure == "release":
        rig.document["engine_flags"] = ["changed"]
    if failure == "health":
        rig.health_ready = False
    assert rig.launch().run() is Outcome.ABANDONED
    assert_result(rig, "ABANDON")
    assert not rig.served.exists()


@pytest.mark.parametrize(("target", "limit"), [("Warmed:Stopped", 900), ("InService", 400)])
def test_deadline_abandons_on_controlled_clock(tmp_path: Path, target: str, limit: int) -> None:
    rig = Rig(tmp_path, target)
    rig.pull_time = 1000
    rig.health_ready = False
    assert rig.launch().run() is Outcome.ABANDONED
    assert rig.now == limit
    assert_result(rig, "ABANDON")


def test_unmeasured_promotion_has_no_agent_deadline(tmp_path: Path) -> None:
    rig = Rig(tmp_path, "InService", measured=None)
    rig.start_time = 1000
    assert rig.launch().run() is Outcome.CONTINUED
    assert_result(rig, "CONTINUE")


def test_health_wait_cannot_overrun_the_promotion_deadline(tmp_path: Path) -> None:
    rig = Rig(tmp_path, "InService")
    rig.start_time = 398
    rig.health_time = 5
    rig.health_ready = False
    assert rig.launch().run() is Outcome.ABANDONED
    assert_result(rig, "ABANDON")
    assert rig.now == 400, "A slow health route must not extend the 400-second deadline"


def test_hung_image_inspection_cannot_outlive_the_promotion_deadline(tmp_path: Path) -> None:
    rig = Rig(tmp_path, "InService", measured=1)
    # Replace only nsenter: the real Host.holds runs this harmless stand-in instead of docker.
    nsenter = tmp_path / "nsenter"
    nsenter.write_text("#!/bin/sh\nexec sleep 3\n")
    nsenter.chmod(0o700)
    agent = replace(
        rig.launch(), host=Host(ecr=object(), nsenter=str(nsenter)),
        clock=time.monotonic, sleep=time.sleep,
    )
    started = time.monotonic()
    assert agent.run() is Outcome.ABANDONED
    elapsed = time.monotonic() - started
    assert_result(rig, "ABANDON")
    # Allow scheduling overhead, but a three-second inspect cannot consume a two-second budget.
    assert elapsed < 2.8, f"Image inspection extended the two-second deadline to {elapsed:.2f}s"
