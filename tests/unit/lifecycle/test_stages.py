"""What each stage of a start took (`rhi_lifecycle.stages`, marked by `rhi_lifecycle.launch`; system
design, section 5.2).

The launches are real ones, on a stand-in for everything around the agent in which each step costs
a known number of seconds of a clock that only the stand-in moves. The durations are read where the
node's collector reads them, on the agent's metrics endpoint, and the stages a launch reached in the
rows of a stand-in for the heartbeats table. No test here needs AWS, docker, the network or a wait.
"""

from __future__ import annotations

import copy
import json
import urllib.request
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError
from prometheus_client import Histogram
from prometheus_client.parser import text_string_to_metric_families

from rhi_common.engine_metrics import MOCK
from rhi_common.records import InstanceHeartbeat
from rhi_lifecycle import metrics, stages
from rhi_lifecycle.host import Host
from rhi_lifecycle.launch import ENGINE_URL, METADATA_URL, NO_MODEL, Engine, Launch, Metadata, Outcome
from rhi_lifecycle.termination import Termination

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NAME = "/rhi/pools/coursework/release"
DIGEST = "sha256:" + "a" * 64
# Made-up values, built here: neither is an instance or a credential.
INSTANCE = "-".join(["i", "0" * 17])
SECRET = "-".join(["the", "pool", "secret", "of", "this", "test"])
VALUE: dict[str, Any] = {
    "engine_image": f"registry.example.com/rhi/engine@{DIGEST}",
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
WARM, IN_SERVICE = "Warmed:Stopped", "InService"
# The time of day when the kernel started, in seconds since the epoch.
EPOCH = 1_800_000_000

# The seconds each step costs: the boot, one read of the release parameter, the host's answer about
# the image, a pull, the read of the secret, the start of the engine's unit, the load of the model,
# the answer to the warm-up request, and the completion of the hook.
BOOT, READ, ASK, PULL, SECRET_READ, UNIT, LOAD, ANSWER, COMPLETE = 40.0, 1.0, 2.0, 300.0, 1.0, 20.0, 60.0, 7.0, 3.0


class World(Host):
    """An instance prepared with the release of `VALUE`, whose kernel started at 0 on the clock.
    `rows` holds the heartbeat rows the launch wrote, `results` what it completed the hook with."""

    def __init__(self, folder: Path, target: str, *, on_disk: bool, measured: int | None = None) -> None:
        super().__init__(ecr=object())
        self.now = BOOT
        self.target, self.on_disk = target, on_disk
        self.parameter = copy.deepcopy(VALUE)
        if measured is not None:
            self.parameter["measured_start_seconds"] = measured
        self.held, self.served = folder / "release.json", folder / "run" / "served"
        self.held.write_text(json.dumps(VALUE))
        # The engine: when its health route starts to answer 200 (None: never), whether its model
        # ever loads, and the status of its answer to the warm-up request.
        self.healthy_at: float | None = None
        self.loads, self.warm_up = True, 200
        self.table_refuses = False
        self.tables: list[str] = []
        self.rows: list[InstanceHeartbeat] = []
        self.results: list[str] = []

    def launch(self) -> Launch:
        metadata = Metadata(self.fetch)
        return Launch(
            NAME,
            metadata=metadata,
            autoscaling=self,
            ssm=self,
            secrets=self,
            dynamodb=self,
            host=self,
            engine=Engine(self.fetch),
            termination=Termination(
                NAME, metadata, autoscaling=self, ssm=self, host=self, engine=MOCK, fetch=self.fetch, sleep=self.sleep
            ),
            held=self.held,
            served=self.served,
            clock=lambda: self.now,
            sleep=self.sleep,
            wall=lambda: EPOCH + self.now,
            uptime=lambda: self.now,
        )

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    # The host module.

    def holds(self, image: str, timeout: float | None = None) -> bool:
        self.now += ASK
        return self.on_disk

    def pull(self, image: str, timeout: float | None = None) -> None:
        self.now += PULL
        self.on_disk = True

    def start_engine(self, timeout: float | None = None) -> None:
        self.now += UNIT
        self.healthy_at = self.now + LOAD if self.loads else None

    # The SDK's clients, and the heartbeats table of its DynamoDB resource.

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        self.now += READ
        return {"Parameter": {"Name": NAME, "Value": json.dumps(self.parameter)}}

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        self.now += SECRET_READ
        return {"SecretString": SECRET}

    def complete_lifecycle_action(self, **request: str) -> dict[str, Any]:
        self.now += COMPLETE
        self.results.append(request["LifecycleActionResult"])
        return {}

    def Table(self, name: str) -> World:
        self.tables.append(name)
        return self

    def put_item(self, **request: Any) -> dict[str, Any]:
        if self.table_refuses:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutItem")
        self.rows.append(InstanceHeartbeat.from_item(request["Item"]))
        return {}

    # The instance metadata and the engine: what answers when the agent opens a URL.

    def fetch(self, request: urllib.request.Request, timeout: float | None) -> tuple[int, bytes] | None:
        url = request.full_url
        if url == f"{METADATA_URL}/api/token":
            return 200, b"the-session-token"
        if url.startswith(METADATA_URL):
            return 200, (self.target if url.endswith("target-lifecycle-state") else INSTANCE).encode()
        if url == f"{ENGINE_URL}/health":
            return (200 if self.healthy_at is not None and self.now >= self.healthy_at else 503), b""
        self.now += ANSWER
        return self.warm_up, b"{}"


def exposed() -> dict[tuple[str, str], float]:
    """The seconds the agent's metrics endpoint gives, by transition and stage."""
    statuses: list[str] = []
    scrape = {"REQUEST_METHOD": "GET", "PATH_INFO": metrics.ROUTE}
    answer = metrics.application()(scrape, lambda status, headers: statuses.append(status))
    assert statuses == ["200 OK"]
    return {
        (sample.labels["transition"], sample.labels["stage"]): sample.value
        for family in text_string_to_metric_families(b"".join(answer).decode())
        for sample in family.samples
        if sample.name == stages.METRIC
    }


def test_a_promotion_gives_one_duration_per_stage_and_they_sum_to_the_whole(tmp_path: Path) -> None:
    world = World(tmp_path, IN_SERVICE, on_disk=True)

    assert world.launch().run() is Outcome.CONTINUED

    assert exposed() == {
        ("promotion", "boot"): BOOT,
        ("promotion", "image"): READ + ASK,
        ("promotion", "release-check"): READ,
        ("promotion", "container-start"): SECRET_READ + UNIT,
        ("promotion", "weights"): LOAD,
        ("promotion", "warm-up"): ANSWER,
        ("promotion", "hook"): COMPLETE,
    }
    assert {stage for _, stage in exposed()} == {stage.value for stage in stages.Stage}
    # The whole: from the kernel's start, at 0 on the clock, to the completed hook.
    assert sum(exposed().values()) == world.now
    # The instance's row named each stage as the launch reached it, and the release it started.
    assert world.tables == ["rhi-heartbeats"]
    reached = [row.stage for row in world.rows]
    assert reached == ["image", "release-check", "container-start", "weights", "warm-up", "hook"]
    assert world.rows[-1] == InstanceHeartbeat(
        instance=INSTANCE,
        pool="coursework",
        running=0,
        waiting=0,
        image_digest=DIGEST,
        model_revision=NO_MODEL,
        stage="hook",
        written_at=int(EPOCH + world.now - COMPLETE),
    )


def another_release(world: World) -> None:
    world.parameter["engine_flags"] = ["--max-model-len", "32768"]


def never_healthy(world: World) -> None:
    world.loads = False


def failed_warm_up(world: World) -> None:
    world.warm_up = 500


@pytest.mark.parametrize(
    ("failure", "last"),
    [(another_release, "release-check"), (never_healthy, "weights"), (failed_warm_up, "warm-up")],
)
def test_an_abandoned_launch_leaves_its_last_stage_in_the_heartbeat(
    failure: Callable[[World], None], last: str, tmp_path: Path
) -> None:
    world = World(tmp_path, IN_SERVICE, on_disk=True, measured=200)
    failure(world)

    assert world.launch().run() is Outcome.ABANDONED

    assert world.results == ["ABANDON"]
    assert world.rows[-1].stage == last
    # The stages it went through are timed up to the moment it was abandoned, and no later one is.
    assert {stage for _, stage in exposed()} == {"boot", *(row.stage for row in world.rows)}
    assert sum(exposed().values()) == world.now - COMPLETE


def test_each_transition_times_the_stages_it_has(tmp_path: Path) -> None:
    (tmp_path / "entry").mkdir()
    entry = World(tmp_path / "entry", WARM, on_disk=False)
    assert entry.launch().run() is Outcome.CONTINUED
    # An entry to the warm pool starts no engine.
    assert exposed() == {
        ("entry", "boot"): BOOT,
        ("entry", "image"): READ + PULL,
        ("entry", "release-check"): READ,
        ("entry", "hook"): COMPLETE,
    }
    assert [row.stage for row in entry.rows] == ["image", "release-check", "hook"]

    # A launch straight into service pulls the image first. The endpoint gives the last launch only.
    (tmp_path / "cold").mkdir()
    cold = World(tmp_path / "cold", IN_SERVICE, on_disk=False)
    assert cold.launch().run() is Outcome.CONTINUED
    assert {transition for transition, _ in exposed()} == {"cold-launch"}
    assert exposed()["cold-launch", "image"] == READ + ASK + PULL
    assert len(exposed()) == len(stages.Stage)
    assert sum(exposed().values()) == cold.now


def test_a_row_that_cannot_be_written_does_not_stop_the_launch(tmp_path: Path) -> None:
    world = World(tmp_path, IN_SERVICE, on_disk=True)
    world.table_refuses = True

    assert world.launch().run() is Outcome.CONTINUED
    assert world.results == ["CONTINUE"]
    assert len(exposed()) == len(stages.Stage)


def test_the_boot_is_what_the_kernel_says_it_has_been_up(tmp_path: Path) -> None:
    (tmp_path / "uptime").write_text("40.25 151.07\n")
    assert stages.uptime(tmp_path / "uptime") == 40.25
    assert stages.uptime(tmp_path / "missing") is None

    # A host that does not say gives the launch no boot stage, and nothing else changes.
    world = World(tmp_path, IN_SERVICE, on_disk=True)
    assert replace(world.launch(), uptime=lambda: None).run() is Outcome.CONTINUED
    assert ("promotion", "boot") not in exposed()
    assert sum(exposed().values()) == world.now - BOOT


def completed(transition: str = "promotion", under: float | None = None) -> float:
    """The launches the agent's endpoint counts as completed, or those of them under `under` seconds."""
    sample, labels = f"{stages.LAUNCH_METRIC}_count", {"transition": transition}
    if under is not None:
        sample, labels = f"{stages.LAUNCH_METRIC}_bucket", {**labels, "le": f"{under:.1f}"}
    return metrics.REGISTRY.get_sample_value(sample, labels) or 0.0


def test_each_launch_completed_is_counted_once_and_no_other(tmp_path: Path) -> None:
    before, before_under = completed(), completed(under=300)
    # Two promotions that took the same seconds are two launches completed.
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
        assert World(tmp_path / name, IN_SERVICE, on_disk=True).launch().run() is Outcome.CONTINUED
    assert completed() == before + 2
    # Each took the sum of its stages, under 5 minutes.
    assert completed(under=300) == before_under + 2

    # A completion that Auto Scaling refused, and an abandoned launch, complete nothing.
    (tmp_path / "refused").mkdir()
    refused = World(tmp_path / "refused", IN_SERVICE, on_disk=True)

    def refuse(**request: str) -> dict[str, Any]:
        refused.now += COMPLETE
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "CompleteLifecycleAction")

    refused.complete_lifecycle_action = refuse  # ty: ignore[invalid-assignment] - a stand-in that refuses
    assert refused.launch().run() is Outcome.NOT_COMPLETED
    (tmp_path / "abandoned").mkdir()
    abandoned = World(tmp_path / "abandoned", IN_SERVICE, on_disk=True, measured=200)
    never_healthy(abandoned)
    assert abandoned.launch().run() is Outcome.ABANDONED
    assert completed() == before + 2


def test_a_launch_of_exactly_five_minutes_is_not_under_them() -> None:
    launches = Histogram("launch_seconds", "", ("transition",), buckets=stages.LAUNCH_BUCKETS, registry=None)
    timings = stages.Timings(stages.Transition.PROMOTION, lambda: 0.0, launches=launches)
    timings.seconds = {stages.Stage.BOOT: 100.0, stages.Stage.HOOK: 200.0}
    timings.completed()
    buckets = {(s.labels["le"], s.value) for s in next(iter(launches.collect())).samples if s.name.endswith("_bucket")}
    assert buckets >= {
        ("300.0", 0.0),
        ("420.0", 1.0),
    }
