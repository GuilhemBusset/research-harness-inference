"""Decides whether an execution of the pipeline verifies its release on the coursework pool (system
design, sections 4.3 and 8.2, step 6). The pipeline's verification step runs it after the stage's
deployment and before the smoke tests (`src/rhi/pipeline/verify.py`).

A deployment does not reach an existing instance by itself: a release reaches a GPU pool's
instances through an instance refresh of the pool's group, which every change of the pool's engine
image, model revision, engine flags or machine image starts, and which an unrelated change does
not start (`rhi.pools.release`). So the question "did this execution change what a GPU pool
serves?" is asked of Auto Scaling: **yes** when an instance refresh of a GPU pool's group began
after the execution started. A GPU pool's group is a group tagged with its pool, `rhi-pool`, whose
engine, its tag `rhi-engine`, is not the mock's (`rhi.pools.group`).

On yes, the script waits for every such refresh to end before it answers, polling every
`POLL_SECONDS` for at most `WAIT_SECONDS`: the smoke tests are only meaningful against the
instances the refresh left. A refresh that ends `Successful` lets the verification go on; while
it is pending, in progress, baking, being cancelled or rolling back it is waited for; any other
end, a failure, a cancellation or a rollback that succeeded or failed, fails the decision, and so
does a refresh still running when the wait is over. Whichever GPU pool's refresh it was, the pool
the smoke tests then run on is the coursework pool, `COURSEWORK_POOL`.

**When the execution started.** CodePipeline gives an action the execution's ID, which the step
passes as `RHI_VERIFY_EXECUTION`, and CodeBuild names the pipeline that started the build in its
own variable `CODEBUILD_INITIATOR`, as `codepipeline/<pipeline name>`. The script reads the
execution's start among the pipeline's executions (`codepipeline:ListPipelineExecutions`). The
pipeline's name is not given to the project in its environment: the pipeline refers to the
project, and a reference back would be a circular dependency.

**What it leaves.** The pools to run the smoke tests on, one per line and nothing else, in the
file `RHI_VERIFY_GPU_POOLS_FILE` names: `coursework`, or nothing when the answer is no. The
verification script reads that file (`verify.py`). A decision that fails leaves no file and exits
non-zero, which fails the step before any test runs.

Run by the verification step, from the checkout:

    uv run --locked python scripts/pipeline/gpu_verification.py
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

NAME = "gpu-verification"

# The pool the smoke tests run on when a GPU pool's release was deployed (section 8.2, step 6).
COURSEWORK_POOL = "coursework"
# The group's tags (`rhi.pools.group`), and the engine of the pool that is not a GPU pool.
POOL_TAG = "rhi-pool"
ENGINE_TAG = "rhi-engine"
MOCK_ENGINE = "mock"

# An instance refresh's statuses (Auto Scaling's own words): the one that lets the verification go
# on, and those of a refresh that has not ended yet. Every other status is an end that fails.
SUCCESSFUL = "Successful"
RUNNING = frozenset({"Pending", "InProgress", "Baking", "Cancelling", "RollbackInProgress"})

# Between two reads of a refresh, and the longest the script waits for the refreshes to end: the
# deployment itself waits up to an hour for a refresh (`rhi.pools.release`).
POLL_SECONDS = 30.0
WAIT_SECONDS = 3600.0
# At most this many pages of the pipeline's executions, of the groups and of a group's refreshes.
PAGES = 20

EXECUTION_VARIABLE = "RHI_VERIFY_EXECUTION"
GPU_POOLS_FILE_VARIABLE = "RHI_VERIFY_GPU_POOLS_FILE"
INITIATOR_VARIABLE = "CODEBUILD_INITIATOR"
INITIATOR_PREFIX = "codepipeline/"


class DecisionError(Exception):
    """The decision cannot be made, or a refresh did not end well. The message names a pool, a
    variable or a status, never a value of the account."""


@dataclass(frozen=True)
class Refresh:
    """An instance refresh of a GPU pool's group."""

    group: str
    pool: str
    refresh_id: str


def _pages(call: Callable[..., dict[str, Any]], token: str, **request: Any) -> list[dict[str, Any]]:
    """The answers of up to `PAGES` calls, each given the previous answer's `token` as its own."""
    answers: list[dict[str, Any]] = []
    for _ in range(PAGES):
        answer = call(**request)
        answers.append(answer)
        if not answer.get(token):
            break
        request[token] = answer[token]
    return answers


def gpu_groups(autoscaling: Any) -> list[tuple[str, str]]:
    """Each GPU pool's group, as `(the group's name, the pool)`: a group tagged with its pool whose
    engine is not the mock's."""
    found = []
    filters = [{"Name": "tag-key", "Values": [ENGINE_TAG]}]
    for page in _pages(autoscaling.describe_auto_scaling_groups, "NextToken", Filters=filters):
        for group in page.get("AutoScalingGroups", []):
            tags = {tag.get("Key"): tag.get("Value") for tag in group.get("Tags", [])}
            if tags.get(POOL_TAG) and tags.get(ENGINE_TAG) not in (None, MOCK_ENGINE):
                found.append((group["AutoScalingGroupName"], tags[POOL_TAG]))
    return found


def refreshes_since(autoscaling: Any, started: datetime) -> list[Refresh]:
    """The instance refreshes of the GPU pools' groups that began after `started`. A refresh that
    has no start time yet has not begun before it."""
    found = []
    for group, pool in gpu_groups(autoscaling):
        pages = _pages(autoscaling.describe_instance_refreshes, "NextToken", AutoScalingGroupName=group)
        for page in pages:
            for refresh in page.get("InstanceRefreshes", []):
                began = refresh.get("StartTime")
                if began is None or began > started:
                    found.append(Refresh(group=group, pool=pool, refresh_id=refresh["InstanceRefreshId"]))
    return found


def status_of(autoscaling: Any, refresh: Refresh) -> str:
    answer = autoscaling.describe_instance_refreshes(
        AutoScalingGroupName=refresh.group, InstanceRefreshIds=[refresh.refresh_id]
    )
    found = answer.get("InstanceRefreshes", [])
    return found[0].get("Status", "unknown") if found else "unknown"


def decide(
    autoscaling: Any,
    started: datetime,
    *,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_seconds: float = POLL_SECONDS,
    wait_seconds: float = WAIT_SECONDS,
) -> list[str]:
    """The pools to run the smoke tests on for an execution that started at `started`:
    `[COURSEWORK_POOL]` once every refresh of a GPU pool's group that began after it has ended
    well, `[]` when there is none. A refresh that ended otherwise, or that is still running after
    `wait_seconds`, raises `DecisionError`. `autoscaling` is an Auto Scaling client of the AWS SDK."""
    found = refreshes_since(autoscaling, started)
    deadline = clock() + wait_seconds
    for refresh in found:
        while (status := status_of(autoscaling, refresh)) in RUNNING:
            if clock() >= deadline:
                raise DecisionError(f"the instance refresh of the pool {refresh.pool} is still {status}")
            sleep(poll_seconds)
        if status != SUCCESSFUL:
            raise DecisionError(f"the instance refresh of the pool {refresh.pool} ended {status}")
        print(f"{NAME}: the instance refresh of the pool {refresh.pool} ended {status}")
    return [COURSEWORK_POOL] if found else []


def pipeline_name(env: Mapping[str, str]) -> str:
    """The name of the pipeline that started the build, from CodeBuild's own variable."""
    initiator = env.get(INITIATOR_VARIABLE, "")
    if not initiator.startswith(INITIATOR_PREFIX) or initiator == INITIATOR_PREFIX:
        raise DecisionError(f"{INITIATOR_VARIABLE} does not name a pipeline: the build was not started by one")
    return initiator.removeprefix(INITIATOR_PREFIX)


def execution_start(codepipeline: Any, pipeline: str, execution: str) -> datetime:
    """When the execution `execution` of the pipeline `pipeline` started. `codepipeline` is a
    CodePipeline client of the AWS SDK."""
    for page in _pages(codepipeline.list_pipeline_executions, "nextToken", pipelineName=pipeline):
        for summary in page.get("pipelineExecutionSummaries", []):
            if summary.get("pipelineExecutionId") == execution and summary.get("startTime") is not None:
                return summary["startTime"]
    raise DecisionError(f"the execution {EXECUTION_VARIABLE} names is not among the pipeline's executions")


def main(
    environ: Mapping[str, str] | None = None,
    autoscaling: Any = None,
    codepipeline: Any = None,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Decides for the execution the environment names, and leaves the pools to verify on in the
    file it names; 0 once they are left. Without a client, the build's own credentials and region
    make it."""
    env = os.environ if environ is None else environ
    named, execution = env.get(GPU_POOLS_FILE_VARIABLE), env.get(EXECUTION_VARIABLE)
    try:
        if not named or not execution:
            raise DecisionError(f"{GPU_POOLS_FILE_VARIABLE} and {EXECUTION_VARIABLE} must both be set")
        pipeline = pipeline_name(env)
        if codepipeline is None or autoscaling is None:
            import boto3

            codepipeline = codepipeline or boto3.client("codepipeline")
            autoscaling = autoscaling or boto3.client("autoscaling")
        started = execution_start(codepipeline, pipeline, execution)
        pools = decide(autoscaling, started, sleep=sleep, clock=clock)
    except DecisionError as error:
        print(f"{NAME}: {error}", file=sys.stderr)
        return 1
    path = Path(named)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{pool}\n" for pool in pools), encoding="utf-8")
    print(f"{NAME}: the smoke tests run on " + (", ".join(pools) if pools else "no GPU pool"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
