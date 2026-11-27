"""The job `engine_contract` (`spikes/engine_contract.py`), on a stand-in for Systems Manager, the
pool-state records of `conftest.py` and a clock the test controls. No AWS, no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import engine_contract as job

T0 = 1_800_000_000.0
# The instances of the documentation's examples, which no line may name.
FIRST, SECOND = "i-1234567890abcdef0", "i-0abcdef1234567890"
PASSING = "\n".join([*(f"rhi-contract {test} passed" for test in job.TESTS), "rhi-contract exit 0"])
UTC_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += seconds


class RunCommand:
    """Stands for Systems Manager: records what is sent, and lists, at each read, the invocations
    the script gives for that read (the last one again once it runs out), two to a page."""

    def __init__(self, *reads: list[tuple[str, str, str]]) -> None:
        self.reads, self.sent, self.read = list(reads), [], 0

    def send_command(self, **sent: Any) -> dict[str, Any]:
        self.sent.append(sent)
        return {"Command": {"CommandId": "a-command"}}

    def list_command_invocations(self, **asked: Any) -> dict[str, Any]:
        assert (asked["CommandId"], asked["Details"]) == ("a-command", True)
        listed = [
            {"InstanceId": instance, "Status": status, "CommandPlugins": [{"Output": output}]}
            for instance, status, output in self.reads[min(self.read, len(self.reads) - 1)]
        ]
        start = int(asked.get("NextToken", 0))
        page = {"CommandInvocations": listed[start : start + 2]}
        if start + 2 < len(listed):
            page["NextToken"] = str(start + 2)
        else:
            self.read += 1
        return page


@pytest.fixture(autouse=True)
def deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed configuration, as much of it as the job reads."""
    pool = SimpleNamespace(name="coursework", engine="vllm")
    monkeypatch.setattr(job.config, "read_ssm", lambda: SimpleNamespace(pools=[pool], session_max_hours=12))


def ran(pool_state: Any, ssm: RunCommand, instances: int = 2) -> tuple[int, list[dict[str, Any]], list[str]]:
    clock = Clock()
    pool_state.book(in_service=instances, now=int(T0))
    written: list[str] = []
    environ = pool_state.environ | {"RHI_SPIKE_INSTANCES": str(instances)}
    status = job.main(environ, out=written.append, ssm=ssm, table=pool_state, now=clock, sleep=clock.sleep)
    assert all(UTC_TIME.fullmatch(json.loads(line)["at"]) for line in written)
    assert not [line for line in written if FIRST in line or SECOND in line]
    return status, [json.loads(line) for line in written], written


def checks(lines: list[dict[str, Any]]) -> list[tuple[int, str, dict[str, Any], int | None]]:
    return [
        (one["instance"], one["status"], one["tests"], one["exit"]) for one in lines if one["kind"] == "instance_check"
    ]


ALL_PASSED = dict.fromkeys(job.TESTS, "passed")


@pytest.mark.parametrize(
    ("in_service", "minutes", "given", "reason"),
    [
        (None, 600, {}, "no-session"),
        (1, 600, {"RHI_SPIKE_INSTANCES": "2"}, "too-few-instances"),
        (1, 30, {}, "session-too-short"),
    ],
)
def test_a_session_that_refuses_the_run_gives_one_line_and_sends_nothing(
    in_service: int | None, minutes: int, given: dict[str, str], reason: str, pool_state: Any
) -> None:
    if in_service is not None:
        pool_state.book(in_service=in_service, minutes=minutes, now=int(T0))
    ssm, written = RunCommand([]), []
    clock = Clock()
    environ = pool_state.environ | given
    assert job.main(environ, out=written.append, ssm=ssm, table=pool_state, now=clock, sleep=clock.sleep) == 1
    (line,) = [json.loads(one) for one in written]
    assert (line["kind"], line["error"], line["reason"]) == ("job_failed", "SessionRefused", reason)
    assert ssm.sent == []


def test_instances_that_all_passed_pass_the_job(pool_state: Any) -> None:
    # The second instance appears one read later, and the first is still running at the first read.
    ssm = RunCommand(
        [(FIRST, "InProgress", "")],
        [(FIRST, "Success", PASSING), (SECOND, "InProgress", "")],
        [(FIRST, "Success", PASSING), (SECOND, "Success", PASSING)],
    )
    status, lines, _ = ran(pool_state, ssm)
    assert status == 0
    assert (lines[0]["kind"], lines[0]["pool"], lines[0]["engine"]) == ("job", "coursework", "vllm")
    assert checks(lines) == [(1, "Success", ALL_PASSED, 0), (2, "Success", ALL_PASSED, 0)]
    # Sent once, the one document, by the pool's tag, to every instance whatever fails.
    (sent,) = ssm.sent
    assert sent["DocumentName"] == "rhi-engine-contract"
    assert sent["Targets"] == [{"Key": "tag:rhi-pool", "Values": ["coursework"]}]
    assert sent["MaxErrors"] == "100%"


def test_an_instance_that_failed_a_test_fails_the_job(pool_state: Any) -> None:
    failed = PASSING.replace("test_metrics passed", "test_metrics failed").replace("exit 0", "exit 1")
    status, lines, _ = ran(pool_state, RunCommand([(FIRST, "Success", PASSING), (SECOND, "Failed", failed)]))
    assert status == 1
    assert checks(lines)[1] == (2, "Failed", ALL_PASSED | {"test_metrics": "failed"}, 1)


def test_an_invocation_that_timed_out_or_never_ended_fails_the_job(pool_state: Any) -> None:
    status, lines, _ = ran(pool_state, RunCommand([(FIRST, "Success", PASSING), (SECOND, "TimedOut", "")]))
    assert status == 1
    assert checks(lines)[1] == (2, "TimedOut", dict.fromkeys(job.TESTS), None)
    # Still running when the run's minutes end: the job stops waiting, and fails.
    status, lines, _ = ran(pool_state, RunCommand([(FIRST, "Success", PASSING), (SECOND, "InProgress", "")]))
    assert status == 1
    assert checks(lines)[1] == (2, "InProgress", dict.fromkeys(job.TESTS), None)


def test_an_invocation_that_appears_after_the_given_instances_ended_is_waited_for(pool_state: Any) -> None:
    failed = PASSING.replace("test_health passed", "test_health error")
    ssm = RunCommand(
        [(FIRST, "Success", PASSING)],
        [(FIRST, "Success", PASSING), (SECOND, "InProgress", "")],
        [(FIRST, "Success", PASSING), (SECOND, "Failed", failed)],
    )
    status, lines, _ = ran(pool_state, ssm, instances=1)
    assert status == 1
    assert checks(lines)[1] == (2, "Failed", ALL_PASSED | {"test_health": "error"}, 0)


def test_fewer_instances_than_given_fail_the_job_once_the_list_settles(pool_state: Any) -> None:
    status, lines, _ = ran(pool_state, RunCommand([(FIRST, "Success", PASSING)]), instances=2)
    assert status == 1 and len(checks(lines)) == 1


def test_only_the_vocabulary_of_the_output_is_written(pool_state: Any) -> None:
    noise = ["private diagnostic sentinel", "rhi-contract test_sentinel passed", "rhi-contract test_health sentinel"]
    noise += ["rhi-contract exit sentinel", "----------ERROR-------", "rhi-contract test_inference passed sentinel"]
    output = "\n".join([*noise, PASSING])
    status, lines, written = ran(pool_state, RunCommand([(FIRST, "Success", output), (SECOND, "Exotic", PASSING)]))
    assert "sentinel" not in "".join(written) and "ERROR" not in "".join(written)
    # A status Run Command does not list is `other`, and does not pass.
    assert checks(lines) == [(1, "Success", ALL_PASSED, 0), (2, "other", ALL_PASSED, 0)]
    assert status == 1


def test_the_vocabulary_is_the_contract_script_s() -> None:
    script = (Path(__file__).resolve().parents[3] / "services/engine/contract/run.sh").read_text(encoding="utf-8")
    (tests,) = re.findall(r"^tests=\(([^)]*)\)$", script, flags=re.MULTILINE)
    assert tuple(tests.split()) == job.TESTS
