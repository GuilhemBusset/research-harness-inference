"""Decides whether an execution of the pipeline verifies its release on the coursework pool (system
design, sections 4.3 and 8.2, step 6). The pipeline's verification step runs it after the stage's
deployment and before the smoke tests (`src/rhi/pipeline/verify.py`).

A deployment replaces no instance: it writes each pool's release parameter (`rhi.pools.release`),
and the next session's instances read it at their start. So the question "did this execution
change what a GPU pool serves?" is asked of the release parameters, the ones the step is given in
`RHI_VERIFY_RELEASE_PARAMETERS`. A GPU pool is one whose engine, `settings.engine` in its
parameter, is not the mock's; the mock pool's parameter is never compared. The answer is **yes**
when, for a GPU pool:

- the parameter's `engine_image`, `model_revision` or `engine_flags` (`COMPARED`, three of the keys
  `rhi_lifecycle.release` fixes) differ from those of its last version written before the
  execution started; or
- the parameter has no version written before the execution started: the pool is deployed for the
  first time, or Parameter Store no longer holds a version that old.

It is **no** otherwise: a parameter whose current version was written before the execution started
was not changed by it, and a change of the agent's `files`, of the measured start or of a setting
books no GPU, since the mock pool's instance runs the same files in the same verification. The
machine image is not in the parameter and is not compared: a new one has its own canary start
before it is promoted. Whichever GPU pool's release changed, the pool the smoke tests then run on
is the coursework pool, `COURSEWORK_POOL`, which the verification script books (`verify.py`). The
decision waits for nothing: the deployment left the pool's group alone.

**Before means earlier.** A version written at the very time the execution started is not one
written before it: it is compared with the last version written earlier, like one written since.

**The earlier versions.** `ssm:GetParameter` reads a version of a parameter by its number, as
`<name>:<version>`, with the time it was written. The script reads the current version, and, when
that one was not written before the execution started, the versions below it one by one, down
from the newest, until one was. So it needs no other action of SSM than the one the step's role
holds on those parameters. It reads at most `VERSIONS` of them, as many as Parameter Store keeps.

**When the execution started.** CodePipeline gives an action the execution's ID, which the step
passes as `RHI_VERIFY_EXECUTION`, and CodeBuild names the pipeline that started the build in its
own variable `CODEBUILD_INITIATOR`, as `codepipeline/<pipeline name>`. The script reads the
execution's start among the pipeline's executions (`codepipeline:ListPipelineExecutions`). The
pipeline's name is not given to the project in its environment: the pipeline refers to the
project, and a reference back would be a circular dependency.

**What it leaves.** The pools to run the smoke tests on, one per line and nothing else, in the
file `RHI_VERIFY_GPU_POOLS_FILE` names: `coursework`, or nothing when the answer is no. The
verification script reads that file (`verify.py`). It also prints one line per pool, which names
the pool and, for a release that changed, the keys that differ, never a value. A decision that
cannot be made, a parameter that cannot be read or that holds no JSON object, leaves no file and
exits non-zero, which fails the step before any test runs.

Run by the verification step, from the checkout:

    uv run --locked python scripts/pipeline/gpu_verification.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

NAME = "gpu-verification"

# The pool the smoke tests run on when a GPU pool's release was deployed (section 8.2, step 6).
COURSEWORK_POOL = "coursework"
# The engine of the pool that is not a GPU pool, as the settings of its release name it.
MOCK_ENGINE = "mock"
# The keys of a release that only a GPU instance can test (`rhi_lifecycle.release`).
COMPARED = ("engine_image", "model_revision", "engine_flags")

# The most versions of one parameter the script reads: Parameter Store keeps a parameter's last
# hundred. SSM's own code for a version it does not hold.
VERSIONS = 100
VERSION_NOT_FOUND = "ParameterVersionNotFound"
# At most this many pages of the pipeline's executions.
PAGES = 20

EXECUTION_VARIABLE = "RHI_VERIFY_EXECUTION"
GPU_POOLS_FILE_VARIABLE = "RHI_VERIFY_GPU_POOLS_FILE"
RELEASE_PARAMETERS_VARIABLE = "RHI_VERIFY_RELEASE_PARAMETERS"
INITIATOR_VARIABLE = "CODEBUILD_INITIATOR"
INITIATOR_PREFIX = "codepipeline/"
# What the pools' output reads while there is no pool (`rhi.pools.stack`).
NO_POOL = "none"


class DecisionError(Exception):
    """The decision cannot be made. The message names a pool or a variable, never a value of the
    account."""


@dataclass(frozen=True)
class Version:
    """One version of a pool's release parameter: its number, when it was written, and the JSON
    object it holds, None when it holds none."""

    number: int
    written: datetime
    release: Mapping[str, Any] | None


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


def release_parameters(text: str) -> dict[str, str]:
    """Each pool's release parameter, by the pool's name, from the text of the pools stack's
    output: `pool=name`, comma-separated, or `NO_POOL`."""
    if not text or text == NO_POOL:
        return {}
    return dict(pair.split("=", 1) for pair in text.split(","))


def read_version(ssm: Any, pool: str, name: str, number: int | None = None) -> Version | None:
    """The version `number` of the parameter `name`, the release parameter of `pool`, or its
    current version; None for a numbered version that SSM does not hold. `ssm` is an SSM client of
    the AWS SDK."""
    try:
        answer = ssm.get_parameter(Name=name if number is None else f"{name}:{number}")
    except (ClientError, BotoCoreError) as error:
        code = error.response.get("Error", {}).get("Code") if isinstance(error, ClientError) else None
        if number is not None and code == VERSION_NOT_FOUND:
            return None
        # The pool's name, never SSM's own words.
        raise DecisionError(f"the release parameter of the pool {pool} cannot be read") from None
    parameter = answer.get("Parameter", {})
    held, written = parameter.get("Version"), parameter.get("LastModifiedDate")
    if not isinstance(held, int) or written is None:
        raise DecisionError(f"the release parameter of the pool {pool} cannot be read")
    try:
        release = json.loads(parameter.get("Value", ""))
    except ValueError:
        release = None
    return Version(held, written, release if isinstance(release, dict) else None)


def last_before(ssm: Any, pool: str, name: str, current: int, started: datetime) -> Version | None:
    """The last version of the parameter `name` written before `started`, among those below the
    version `current`; None when SSM holds none."""
    for number in range(current - 1, max(0, current - 1 - VERSIONS), -1):
        version = read_version(ssm, pool, name, number)
        if version is None:
            return None
        if version.written < started:
            return version
    return None


def decide(ssm: Any, parameters: Mapping[str, str], started: datetime) -> list[str]:
    """The pools to run the smoke tests on for an execution that started at `started`, given each
    pool's release parameter by the pool's name: `[COURSEWORK_POOL]` when a GPU pool's parameter
    differs in a key of `COMPARED` from its last version written before `started`, or has no such
    version; `[]` otherwise. A parameter that cannot be read, or whose current version holds no
    JSON object, raises `DecisionError`. `ssm` is an SSM client of the AWS SDK."""
    changed = False
    for pool, name in parameters.items():
        current = read_version(ssm, pool, name)
        if current is None or current.release is None:
            raise DecisionError(f"the release parameter of the pool {pool} holds no release")
        settings = current.release.get("settings")
        if isinstance(settings, dict) and settings.get("engine") == MOCK_ENGINE:
            print(f"{NAME}: the pool {pool} runs the mock engine: its release is not compared")
            continue
        if current.written < started:
            print(f"{NAME}: the release of the pool {pool} was not written since the execution started")
            continue
        before = last_before(ssm, pool, name, current.number, started)
        if before is None:
            print(f"{NAME}: the release of the pool {pool} has no version written before the execution started")
            changed = True
            continue
        earlier = before.release
        differing = [key for key in COMPARED if earlier is None or current.release.get(key) != earlier.get(key)]
        if differing:
            print(f"{NAME}: the release of the pool {pool} changed: {', '.join(differing)}")
            changed = True
        else:
            print(f"{NAME}: the release of the pool {pool} changed nothing that only a GPU instance can test")
    return [COURSEWORK_POOL] if changed else []


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


def main(environ: Mapping[str, str] | None = None, ssm: Any = None, codepipeline: Any = None) -> int:
    """Decides for the execution the environment names, and leaves the pools to verify on in the
    file it names; 0 once they are left. Without a client, the build's own credentials and region
    make it."""
    env = os.environ if environ is None else environ
    named, execution = env.get(GPU_POOLS_FILE_VARIABLE), env.get(EXECUTION_VARIABLE)
    try:
        if not named or not execution:
            raise DecisionError(f"{GPU_POOLS_FILE_VARIABLE} and {EXECUTION_VARIABLE} must both be set")
        pipeline = pipeline_name(env)
        if codepipeline is None or ssm is None:
            import boto3

            codepipeline = codepipeline or boto3.client("codepipeline")
            ssm = ssm or boto3.client("ssm")
        started = execution_start(codepipeline, pipeline, execution)
        pools = decide(ssm, release_parameters(env.get(RELEASE_PARAMETERS_VARIABLE, "")), started)
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
