"""The decision whether an execution verifies on the coursework pool (scripts/pipeline/gpu_verification.py):
yes when an instance refresh of a GPU pool's group began after the execution started, once that
refresh has ended well; it waits for a refresh that is running and fails on one that ended any
other way. Stand-ins for the Auto Scaling and CodePipeline clients replace AWS, and a fake clock
replaces time: no account, no credentials, no network, no wait.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

import gpu_verification as decision

if TYPE_CHECKING:
    from pathlib import Path

STARTED = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
AFTER, BEFORE = STARTED + timedelta(minutes=5), STARTED - timedelta(minutes=5)


def group(name: str, pool: str, engine: str) -> dict[str, Any]:
    return {
        "AutoScalingGroupName": name,
        "Tags": [{"Key": "rhi-pool", "Value": pool}, {"Key": "rhi-engine", "Value": engine}],
    }


GROUPS = [group("coursework-group", "coursework", "vllm"), group("mock-group", "mock", "mock")]


class AutoScaling:
    """An Auto Scaling client as far as the decision uses one: `groups`, and for each group's name
    its refreshes, as `(ID, start time)`. A refresh's status is read from `statuses`, one status a
    read, the last one repeated."""

    def __init__(
        self,
        refreshes: dict[str, list[tuple[str, datetime]]],
        statuses: dict[str, list[str]] | None = None,
        groups: list[dict[str, Any]] = GROUPS,
    ) -> None:
        self.groups, self.refreshes, self.statuses = groups, refreshes, statuses or {}
        self.reads: list[str] = []

    def describe_auto_scaling_groups(self, **request: Any) -> dict[str, Any]:
        assert request["Filters"] == [{"Name": "tag-key", "Values": ["rhi-engine"]}]
        return {"AutoScalingGroups": self.groups}

    def describe_instance_refreshes(self, **request: Any) -> dict[str, Any]:
        held = self.refreshes.get(request["AutoScalingGroupName"], [])
        if "InstanceRefreshIds" not in request:
            return {"InstanceRefreshes": [{"InstanceRefreshId": rid, "StartTime": at} for rid, at in held]}
        (refresh_id,) = request["InstanceRefreshIds"]
        self.reads.append(refresh_id)
        statuses = self.statuses.get(refresh_id, [decision.SUCCESSFUL])
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return {"InstanceRefreshes": [{"InstanceRefreshId": refresh_id, "Status": status}]}


class Clock:
    """Time that passes only when the decision sleeps."""

    def __init__(self) -> None:
        self.now, self.slept = 0.0, []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def decide(autoscaling: AutoScaling, clock: Clock | None = None) -> list[str]:
    clock = clock or Clock()
    return decision.decide(autoscaling, STARTED, sleep=clock.sleep, clock=clock)


def test_a_refresh_of_the_coursework_group_begun_after_the_start_asks_for_the_coursework_pool() -> None:
    assert decide(AutoScaling({"coursework-group": [("r-1", AFTER)]})) == ["coursework"]


@pytest.mark.parametrize(
    "refreshes",
    [
        {"coursework-group": [("r-1", BEFORE)]},
        {"mock-group": [("r-1", AFTER)]},
        {},
    ],
    ids=["a refresh begun before the start", "a refresh of the mock pool's group", "no refresh"],
)
def test_no_gpu_refresh_since_the_start_asks_for_nothing(refreshes: dict[str, list[tuple[str, datetime]]]) -> None:
    autoscaling = AutoScaling(refreshes)
    assert decide(autoscaling) == []
    # Nothing is waited for.
    assert autoscaling.reads == []


def test_another_gpu_pool_s_refresh_still_verifies_on_the_coursework_pool() -> None:
    groups = [*GROUPS, group("upgrade-group", "upgrade", "vllm")]
    assert decide(AutoScaling({"upgrade-group": [("r-2", AFTER)]}, groups=groups)) == ["coursework"]


def test_a_refresh_in_progress_is_waited_for() -> None:
    statuses = {"r-1": ["Pending", "InProgress", "Baking", "RollbackInProgress", "Successful"]}
    autoscaling, clock = AutoScaling({"coursework-group": [("r-1", AFTER)]}, statuses), Clock()
    assert decide(autoscaling, clock) == ["coursework"]
    assert autoscaling.reads == ["r-1"] * 5
    assert clock.slept == [decision.POLL_SECONDS] * 4


@pytest.mark.parametrize("ended", ["Failed", "Cancelled", "RollbackSuccessful", "RollbackFailed"])
def test_a_refresh_that_did_not_end_successful_fails_the_decision(ended: str) -> None:
    autoscaling = AutoScaling({"coursework-group": [("r-1", AFTER)]}, {"r-1": ["InProgress", ended]})
    with pytest.raises(decision.DecisionError, match=f"coursework ended {ended}"):
        decide(autoscaling)


def test_a_refresh_still_running_after_the_wait_fails_the_decision() -> None:
    autoscaling, clock = AutoScaling({"coursework-group": [("r-1", AFTER)]}, {"r-1": ["InProgress"]}), Clock()
    with pytest.raises(decision.DecisionError, match="still InProgress"):
        decide(autoscaling, clock)
    assert clock.now >= decision.WAIT_SECONDS


class CodePipeline:
    """A CodePipeline client as far as the decision uses one: the pipeline `pipeline`'s executions,
    two pages of one each."""

    def __init__(self, pipeline: str) -> None:
        self.pipeline = pipeline

    def list_pipeline_executions(self, **request: Any) -> dict[str, Any]:
        assert request["pipelineName"] == self.pipeline
        if request.get("nextToken") != "second":
            return {
                "pipelineExecutionSummaries": [{"pipelineExecutionId": "e-new", "startTime": AFTER}],
                "nextToken": "second",
            }
        return {"pipelineExecutionSummaries": [{"pipelineExecutionId": "e-1", "startTime": STARTED}]}


def run_main(tmp_path: Path, autoscaling: AutoScaling, **more: str) -> tuple[int, str | None]:
    """The decision's exit, for the execution `e-1` of the pipeline `a-pipeline`, and the file it
    left, None for none."""
    left = tmp_path / "build" / "gpu-pools"
    environ = {
        "CODEBUILD_INITIATOR": "codepipeline/a-pipeline",
        decision.EXECUTION_VARIABLE: "e-1",
        decision.GPU_POOLS_FILE_VARIABLE: str(left),
        **more,
    }
    clock = Clock()
    exit_code = decision.main(
        environ, autoscaling=autoscaling, codepipeline=CodePipeline("a-pipeline"), sleep=clock.sleep, clock=clock
    )
    return exit_code, left.read_text(encoding="utf-8") if left.exists() else None


def test_the_execution_s_start_is_read_from_the_pipeline_codebuild_names(tmp_path: Path) -> None:
    # A refresh begun after the execution `e-1` started, and before the next one did: its start
    # is that of `e-1`, on the second page, not the first execution's.
    between = STARTED + timedelta(minutes=1)
    assert run_main(tmp_path, AutoScaling({"coursework-group": [("r-1", between)]})) == (0, "coursework\n")
    assert run_main(tmp_path, AutoScaling({})) == (0, "")


def test_a_failed_decision_leaves_no_file_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = AutoScaling({"coursework-group": [("r-1", AFTER)]}, {"r-1": ["Failed"]})
    assert run_main(tmp_path, failed) == (1, None)
    assert run_main(tmp_path, AutoScaling({}), CODEBUILD_INITIATOR="someone") == (1, None)
    assert run_main(tmp_path, AutoScaling({}), **{decision.EXECUTION_VARIABLE: "e-unknown"}) == (1, None)
    assert "coursework ended Failed" in capsys.readouterr().err
