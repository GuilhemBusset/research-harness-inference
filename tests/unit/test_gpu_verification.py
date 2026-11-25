"""The decision whether an execution verifies on the coursework pool (scripts/pipeline/gpu_verification.py):
yes when a GPU pool's release parameter differs, in its engine image, model revision or engine
flags, from its last version written before the execution started, or has no such version; no for
any other change, for a parameter not written since the start, and for the mock pool. Stand-ins
for the SSM and CodePipeline clients replace AWS: no account, no credentials, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

import gpu_verification as decision

if TYPE_CHECKING:
    from pathlib import Path

STARTED = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
AFTER, BEFORE = STARTED + timedelta(minutes=5), STARTED - timedelta(minutes=5)
LONG_BEFORE = STARTED - timedelta(days=1)

COURSEWORK, MOCK = "/rhi/pools/coursework/release", "/rhi/pools/mock/release"
PARAMETERS = {"mock": MOCK, "coursework": COURSEWORK}


def release(engine: str = "vllm", **changed: Any) -> str:
    """A release parameter's value for a pool of the engine `engine`, with `changed` in place of
    its keys; `settings` and `files` are merged into those the release has."""
    document: dict[str, Any] = {
        "engine_image": "registry.example.com/engine@sha256:" + "a" * 64,
        "model_revision": "revision-1",
        "engine_flags": ["--max-model-len", "8192"],
        "files": {"bucket": "assets", "key": "release.tar", "sha256": "b" * 64},
        "measured_start_seconds": 300,
        "settings": {"pool": "coursework", "log_group": "/rhi/pools/coursework", "engine": engine},
    }
    for key, value in changed.items():
        document[key] = document[key] | value if key in ("files", "settings") else value
    return json.dumps(document)


class Ssm:
    """An SSM client as far as the decision uses one: for each parameter's name its versions, the
    first being version `first`, as `(when it was written, its value)`. It offers `get_parameter`
    and no other call, and holds no version below `first`."""

    def __init__(self, versions: dict[str, list[tuple[datetime, str]]], first: int = 1) -> None:
        self.versions, self.first = versions, first
        self.read: list[str] = []

    def get_parameter(self, **request: Any) -> dict[str, Any]:
        assert list(request) == ["Name"]
        self.read.append(request["Name"])
        name, _, selector = request["Name"].partition(":")
        held = self.versions.get(name)
        if not held:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        number = int(selector) if selector else self.first + len(held) - 1
        if not self.first <= number < self.first + len(held):
            raise ClientError({"Error": {"Code": "ParameterVersionNotFound"}}, "GetParameter")
        written, value = held[number - self.first]
        return {"Parameter": {"Name": name, "Value": value, "Version": number, "LastModifiedDate": written}}


def decide(versions: list[tuple[datetime, str]], **more: list[tuple[datetime, str]]) -> list[str]:
    """The decision for the coursework pool's parameter with `versions`, beside a mock pool's that
    the execution left alone."""
    held = {MOCK: [(BEFORE, release("mock"))], COURSEWORK: versions, **more}
    return decision.decide(Ssm(held), PARAMETERS, STARTED)


@pytest.mark.parametrize(
    "changed",
    [
        {"engine_image": "registry.example.com/engine@sha256:" + "c" * 64},
        {"model_revision": "revision-2"},
        {"engine_flags": ["--max-model-len", "16384"]},
    ],
    ids=["engine_image", "model_revision", "engine_flags"],
)
def test_a_gpu_pool_s_release_rewritten_with_another_engine_names_the_coursework_pool(changed: dict[str, Any]) -> None:
    assert decide([(BEFORE, release()), (AFTER, release(**changed))]) == ["coursework"]


@pytest.mark.parametrize(
    "changed",
    [
        {"files": {"sha256": "d" * 64}},
        {"settings": {"log_group": "/rhi/pools/other"}},
        {"measured_start_seconds": 240},
    ],
    ids=["files", "a setting", "the measured start"],
)
def test_a_release_rewritten_with_only_other_files_or_settings_names_nothing(changed: dict[str, Any]) -> None:
    assert decide([(BEFORE, release()), (AFTER, release(**changed))]) == []


def test_a_release_not_written_since_the_start_names_nothing() -> None:
    # Its last two versions differ, and both were written before the execution started.
    versions = [(LONG_BEFORE, release()), (BEFORE, release(model_revision="revision-2"))]
    ssm = Ssm({COURSEWORK: versions})
    assert decision.decide(ssm, {"coursework": COURSEWORK}, STARTED) == []
    # The current version says so: no earlier one is read.
    assert ssm.read == [COURSEWORK]


def test_a_release_is_compared_with_its_last_version_from_before_the_start() -> None:
    # Written twice since the start, once after a failed deployment's rollback: what counts is the
    # version the execution found, not the one just below the current one.
    back, new = release(), release(model_revision="revision-2")
    assert decide([(LONG_BEFORE, new), (BEFORE, back), (AFTER, new), (AFTER, back)]) == []
    assert decide([(LONG_BEFORE, back), (BEFORE, back), (AFTER, back), (AFTER, new)]) == ["coursework"]


def test_a_version_written_at_the_start_is_not_one_from_before_it() -> None:
    # The version written at the very time of the start is compared with the one before it,
    assert decide([(BEFORE, release()), (STARTED, release(model_revision="revision-2"))]) == ["coursework"]
    assert decide([(BEFORE, release()), (STARTED, release())]) == []
    # and is no baseline for a later one.
    new = release(model_revision="revision-2")
    assert decide([(BEFORE, release()), (STARTED, new), (AFTER, new)]) == ["coursework"]


@pytest.mark.parametrize(
    ("versions", "first"),
    [
        ([(AFTER, release())], 1),
        ([(STARTED, release()), (AFTER, release())], 1),
        ([(AFTER, release()), (AFTER, release())], 7),
    ],
    ids=["a first version", "every version since the start", "the earlier versions no longer held"],
)
def test_a_release_with_no_version_from_before_the_start_names_the_coursework_pool(
    versions: list[tuple[datetime, str]], first: int
) -> None:
    assert decision.decide(Ssm({COURSEWORK: versions}, first), {"coursework": COURSEWORK}, STARTED) == ["coursework"]


def test_the_mock_pool_s_release_alone_names_nothing() -> None:
    rewritten = release("mock", engine_image="registry.example.com/mock@sha256:" + "c" * 64)
    for versions in ([(BEFORE, release("mock")), (AFTER, rewritten)], [(AFTER, rewritten)]):
        ssm = Ssm({MOCK: versions, COURSEWORK: [(BEFORE, release())]})
        assert decision.decide(ssm, PARAMETERS, STARTED) == []
        # The mock pool's earlier versions are not read.
        assert ssm.read == [MOCK, COURSEWORK]


def test_another_gpu_pool_s_release_still_verifies_on_the_coursework_pool() -> None:
    upgrade = "/rhi/pools/upgrade/release"
    ssm = Ssm(
        {
            COURSEWORK: [(BEFORE, release())],
            upgrade: [(BEFORE, release()), (AFTER, release(model_revision="revision-2"))],
        }
    )
    assert decision.decide(ssm, {"coursework": COURSEWORK, "upgrade": upgrade}, STARTED) == ["coursework"]


def test_no_pool_names_nothing() -> None:
    assert decision.decide(Ssm({}), decision.release_parameters("none"), STARTED) == []


@pytest.mark.parametrize(
    "versions",
    [[], [(AFTER, "not a release")], [(AFTER, json.dumps(["a", "list"]))]],
    ids=["no parameter", "a value that is not JSON", "a value that is no object"],
)
def test_a_release_that_cannot_be_read_fails_the_decision(versions: list[tuple[datetime, str]]) -> None:
    with pytest.raises(decision.DecisionError, match="the pool coursework"):
        decision.decide(Ssm({COURSEWORK: versions}), {"coursework": COURSEWORK}, STARTED)


def test_an_earlier_version_that_holds_no_release_counts_as_another() -> None:
    assert decide([(BEFORE, "not a release"), (AFTER, release())]) == ["coursework"]


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


def run_main(tmp_path: Path, versions: list[tuple[datetime, str]], **more: str) -> tuple[int, str | None]:
    """The decision's exit, for the execution `e-1` of the pipeline `a-pipeline` and a coursework
    pool whose parameter has `versions`, and the file it left, None for none."""
    left = tmp_path / "build" / "gpu-pools"
    environ = {
        "CODEBUILD_INITIATOR": "codepipeline/a-pipeline",
        decision.EXECUTION_VARIABLE: "e-1",
        decision.GPU_POOLS_FILE_VARIABLE: str(left),
        decision.RELEASE_PARAMETERS_VARIABLE: f"mock={MOCK},coursework={COURSEWORK}",
        **more,
    }
    ssm = Ssm({MOCK: [(BEFORE, release("mock"))], COURSEWORK: versions})
    exit_code = decision.main(environ, ssm=ssm, codepipeline=CodePipeline("a-pipeline"))
    return exit_code, left.read_text(encoding="utf-8") if left.exists() else None


def test_the_execution_s_start_is_read_from_the_pipeline_codebuild_names(tmp_path: Path) -> None:
    # A release written after the execution `e-1` started, and before the next one did: its start
    # is that of `e-1`, on the second page, not the first execution's.
    between = STARTED + timedelta(minutes=1)
    changed = [(BEFORE, release()), (between, release(model_revision="revision-2"))]
    assert run_main(tmp_path, changed) == (0, "coursework\n")
    assert run_main(tmp_path, [(BEFORE, release())]) == (0, "")
    # With no pool, as the pools stack's output reads then, there is nothing to compare.
    assert run_main(tmp_path, changed, **{decision.RELEASE_PARAMETERS_VARIABLE: "none"}) == (0, "")


def test_a_failed_decision_leaves_no_file_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(tmp_path, []) == (1, None)
    assert "the pool coursework" in capsys.readouterr().err
    kept = [(BEFORE, release())]
    assert run_main(tmp_path, kept, CODEBUILD_INITIATOR="someone") == (1, None)
    assert run_main(tmp_path, kept, **{decision.EXECUTION_VARIABLE: "e-unknown"}) == (1, None)
