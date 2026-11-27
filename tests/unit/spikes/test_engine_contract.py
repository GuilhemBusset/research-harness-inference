"""The job `engine_contract` (`spikes/engine_contract.py`), on a stand-in for Systems Manager, the
pool-state records of `conftest.py` and a clock the test controls. No AWS, no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import engine_contract as job

if TYPE_CHECKING:
    from collections.abc import Sequence

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

    def __init__(self, *reads: Sequence[tuple[str, str, str]]) -> None:
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


def ran(
    pool_state: Any, ssm: Any, instances: int = 2, clock: Clock | None = None, **given: str
) -> tuple[int, list[dict[str, Any]], list[str]]:
    clock = Clock() if clock is None else clock
    pool_state.book(in_service=max(instances, 2), now=int(T0))
    written: list[str] = []
    environ = pool_state.environ | {"RHI_SPIKE_INSTANCES": str(instances)} | given
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


def test_an_invocation_that_appears_late_in_the_run_is_recorded(pool_state: Any) -> None:
    # The given instance passed at once; the tag reaches a second, listed only after 90 seconds.
    failed = PASSING.replace("test_health passed", "test_health error")
    ssm = RunCommand(*[[(FIRST, "Success", PASSING)]] * 9, [(FIRST, "Success", PASSING), (SECOND, "Failed", failed)])
    status, lines, _ = ran(pool_state, ssm, instances=1, RHI_SPIKE_RUN_MINUTES="2")
    assert status == 1
    assert checks(lines) == [(1, "Success", ALL_PASSED, 0), (2, "Failed", ALL_PASSED | {"test_health": "error"}, 0)]


class Slow(RunCommand):
    """Run Command whose invocation ends at `ends` seconds into the run, and which takes 20 seconds
    to answer the send, or the read made at 50 seconds."""

    def __init__(self, clock: Clock, slow: str, ends: float) -> None:
        super().__init__()
        self.clock, self.slow, self.ends = clock, slow, T0 + ends

    def send_command(self, **sent: Any) -> dict[str, Any]:
        self.clock.sleep(20 if self.slow == "send" else 0)
        return super().send_command(**sent)

    def list_command_invocations(self, **asked: Any) -> dict[str, Any]:
        if self.slow == "read" and self.clock() == T0 + 50:
            self.clock.sleep(20)
        ended = self.clock() >= self.ends
        self.reads = [[(FIRST, "Success", PASSING) if ended else (FIRST, "InProgress", "")]]
        return super().list_command_invocations(**asked)


@pytest.mark.parametrize("slow", ["send", "read"])
def test_the_run_s_minutes_start_when_the_session_admits_it(slow: str, pool_state: Any) -> None:
    # A run of one minute, whose one invocation ends at 70 seconds: too late, whether the minute
    # went on a slow send or on a read that came back after it.
    clock = Clock()
    status, lines, _ = ran(pool_state, Slow(clock, slow, ends=70), 1, clock, RHI_SPIKE_RUN_MINUTES="1")
    assert status == 1 and checks(lines) == [(1, "InProgress", dict.fromkeys(job.TESTS), None)]
    assert clock() - T0 <= (60 if slow == "send" else 70)
    # Ended at 40 seconds, it passes.
    clock = Clock()
    status, _, _ = ran(pool_state, Slow(clock, slow, ends=40), 1, clock, RHI_SPIKE_RUN_MINUTES="1")
    assert status == 0


class Failing(RunCommand):
    """Run Command whose listing fails from its second call on, with a text no line may carry."""

    def list_command_invocations(self, **asked: Any) -> dict[str, Any]:
        if self.read or "NextToken" in asked:
            raise OSError("an unavailable sentinel")
        return super().list_command_invocations(**asked)


@pytest.mark.parametrize("listed", [1, 3], ids=["on the next read", "on the next page"])
def test_a_failed_read_keeps_what_was_read_before_it(listed: int, pool_state: Any) -> None:
    # One instance listed, and the next read fails; or three, two to a page, and the second page fails.
    status, lines, written = ran(
        pool_state,
        Failing([(FIRST, "Success", PASSING), (SECOND, "Success", PASSING), (FIRST, "Success", PASSING)][:listed]),
    )
    assert status == 1
    assert checks(lines) == [(1, "Success", ALL_PASSED, 0), (2, "Success", ALL_PASSED, 0)][: min(listed, 2)]
    assert (lines[-1]["kind"], lines[-1]["error"]) == ("job_failed", "OSError")
    assert "sentinel" not in "".join(written)


def test_fewer_instances_than_given_fail_the_job_when_the_run_s_minutes_end(pool_state: Any) -> None:
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
