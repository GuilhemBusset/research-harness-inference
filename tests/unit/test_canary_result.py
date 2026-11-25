"""The canary's result (scripts/pipeline/canary_result.py): the session it books on the coursework
pool when the pool has none, its wait for the session's instance, the run of the smoke tests, the
stop of the session it booked, and the measurement gpu-canary it sends, on the commit the job ran
from, with the UTC date of the run as its run label. Each of the three results has its verdict and
its numbers: the tests ran and passed; a test failed, or an instance was launched and did not
serve by the end of the wait; no instance was launched during the wait.

Stand-ins replace AWS and the run of the tests, and moto holds the pool-state table and the
heartbeats table in process: no account, no credentials, no network. A clock the test controls
stands for time: its `sleep` moves it forward.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import canary_result
import verify
from rhi_common.records import SESSION_RECORD, InstanceHeartbeat, Session
from rhi_common.tables import HEARTBEATS as HEARTBEATS_SCHEMA
from rhi_common.tables import POOL_STATE
from rhi_reporter.measure import read_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BUS = "rhi-measurements"
WORKSPACE = "https://aps-workspaces.us-east-1.example.com/workspaces/ws-example/"
HEARTBEATS = "Rhi-Pools-TablesHeartbeatsEXAMPLE-SUFFIX"
PARAMETER = "/rhi/pools/coursework/release"
REPO = Path(__file__).resolve().parents[2]
NAME = re.compile(r"[a-z0-9_]+")
SUNDAY = datetime(2026, 11, 22, 4, 0, 7, tzinfo=UTC)
# When the job begins, in seconds since the epoch, and the wait the job is given, 45 minutes.
NOW = int(SUNDAY.timestamp())
WAIT_SECONDS = 45 * 60
REGION = "us-east-1"
POOLS_STACK = "Rhi-Pools"
POOL_STATE_LOGICAL_ID = "TablesPoolState68A872F7"
POOL_STATE_TABLE = "Rhi-Pools-TablesPoolState68A872F7-EXAMPLE"

# Results as pytest's --junitxml records them: three smoke tests passed, one of them parametrized.
ALL_PASSED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="3">
<testcase classname="tests.smoke.test_smoke" name="test_the_hello_route_answers" time="0.120" />
<testcase classname="tests.smoke.test_smoke" name="test_the_first_stream_wakes_a_cold_pool" time="412.5" />
<testcase classname="tests.smoke.test_smoke" name="test_a_stream_completes_with_its_usage[/v1/messages]" time="3.2" />
</testsuite></testsuites>"""
# The same run, where one test failed and the internal ones were deselected, which records nothing.
ONE_FAILED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="3">
<testcase classname="tests.smoke.test_smoke" name="test_the_hello_route_answers" time="0.120" />
<testcase classname="tests.smoke.test_smoke" name="test_the_first_stream_wakes_a_cold_pool" time="412.5">
<failure message="assert 503 == 200">the pool did not answer</failure></testcase>
<testcase classname="tests.smoke.test_smoke" name="test_a_stream_completes_with_its_usage[/v1/messages]" time="3.2" />
</testsuite></testsuites>"""
# A run pytest did not finish: one test ended, and the one that was running is recorded unnamed.
INTERRUPTED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1">
<testcase classname="tests.smoke.test_smoke" name="test_the_hello_route_answers" time="0.120" />
<testcase time="0.000" />
</testsuite></testsuites>"""


def detail(entry: Mapping[str, str]) -> dict[str, Any]:
    assert entry["Source"] == "rhi.measurement"
    assert entry["EventBusName"] == BUS
    return json.loads(entry["Detail"])


def test_every_name_is_lower_case_letters_digits_and_underscores() -> None:
    results, wait = canary_result.read_results(ALL_PASSED), canary_result.Wait(seconds=1.5)
    found = detail(canary_result.measurement(results, wait, commit=COMMIT, bus=BUS, now=SUNDAY))
    assert found["names"] == ["wait_seconds", "smoke_tests_passed", "smoke_tests_failed", "no_capacity"]
    assert set(found["numbers"]) == set(found["names"])
    assert all(NAME.fullmatch(name) for name in found["names"])
    # The reporter builds its status from the event as it is sent.
    (status,) = read_event({"detail": found})
    assert status.context == "measure/gpu-canary/2026-11-22/1"


@pytest.mark.parametrize(
    ("now", "label"),
    [
        pytest.param(SUNDAY, "2026-11-22", id="in UTC"),
        # Late on Saturday five hours west of UTC is already Sunday in UTC.
        pytest.param(datetime(2026, 11, 21, 23, 30, tzinfo=timezone(timedelta(hours=-5))), "2026-11-22", id="west"),
    ],
)
def test_the_run_label_is_the_utc_date_of_the_clock(now: datetime, label: str) -> None:
    assert canary_result.run_label(now) == label
    sent: list[dict[str, Any]] = []
    canary_result.main(environ() | {"RHI_SMOKE_ENDPOINT": "none"}, clock=now.timestamp, events=Events(sent))
    (entry,) = sent
    assert detail(entry)["label"] == label


class Events:
    """An EventBridge client as far as the script uses one: it keeps each entry, and refuses them
    when `failed` is set, as EventBridge does, with HTTP 200 and a count."""

    def __init__(self, sent: list[dict[str, Any]], *, failed: bool = False) -> None:
        self.sent, self.failed = sent, failed

    def put_events(self, *, Entries: list[dict[str, Any]]) -> dict[str, Any]:
        if self.failed:
            return {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalException"}]}
        self.sent += Entries
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}


class Secrets:
    def __init__(self, held: str | None) -> None:
        self.held = held

    def get_secret_value(self, **_request: Any) -> dict[str, Any]:
        if self.held is None:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "-"}}, "GetSecretValue")
        return {"SecretString": self.held}


class Parameters:
    """An SSM client that holds the coursework pool's release parameter, as the pools stack writes
    it (`rhi.pools.release`)."""

    def get_parameter(self, *, Name: str) -> dict[str, Any]:
        assert Name == PARAMETER
        settings = {
            "pool": "coursework",
            "group": "rhi-coursework",
            "launch_hook": "launch",
            "termination_hook": "termination",
            "log_group": "/rhi/pools/coursework",
            "workspace_url": f"{WORKSPACE}api/v1/remote_write",
            "heartbeat_table": HEARTBEATS,
            "secret_id": "rhi/pools/coursework",
        }
        release = {
            "engine_image": f"registry.example.com/engine@sha256:{'1' * 64}",
            "model_revision": "2" * 40,
            "engine_flags": [],
            "files": {"bucket": "files", "key": "release.tar", "sha256": "3" * 64},
            "settings": settings,
        }
        return {"Parameter": {"Name": Name, "Value": json.dumps(release)}}


def environ() -> dict[str, str]:
    """What the job gives: the commit, the bus, the endpoint, the release parameters, and what the
    booking needs, a wait of 45 minutes and a session of 12 hours at most."""
    return {
        "CODEBUILD_RESOLVED_SOURCE_VERSION": COMMIT,
        "RHI_MEASUREMENT_BUS": BUS,
        "RHI_SMOKE_ENDPOINT": "canary.example.com",
        "RHI_SMOKE_RELEASE_PARAMETERS": json.dumps({"coursework": PARAMETER}),
        "RHI_VERIFY_POOLS_STACK": POOLS_STACK,
        "RHI_VERIFY_POOL_STATE_LOGICAL_ID": POOL_STATE_LOGICAL_ID,
        "RHI_VERIFY_CHECK_WAIT_MINUTES": "45",
        "RHI_VERIFY_SESSION_MAX_HOURS": "12",
    }


def create_table(name: str, schema: Any) -> None:
    """A table of that name, in moto, with the key schema of `schema`, a table of `rhi_common.tables`."""
    keys = [(schema.partition_key, "HASH"), (schema.sort_key, "RANGE")]
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": key.name, "AttributeType": key.type.value} for key, _ in keys if key is not None
        ],
        KeySchema=[{"AttributeName": key.name, "KeyType": kind} for key, kind in keys if key is not None],
    )


@pytest.fixture(autouse=True)
def pool_state() -> Iterator[Any]:
    """The stage's pool-state table and its heartbeats table, in moto, with no item: the pool-state
    table, which every test of the script has."""
    with mock_aws():
        create_table(POOL_STATE_TABLE, POOL_STATE)
        create_table(HEARTBEATS, HEARTBEATS_SCHEMA)
        yield boto3.resource("dynamodb", region_name=REGION).Table(POOL_STATE_TABLE)


def session_of(pool_state: Any, pool: str = "coursework") -> Session | None:
    """The session of the coursework pool, or of another, as the table holds it, None for no item."""
    item = pool_state.get_item(Key={"pool": pool, "record": SESSION_RECORD}, ConsistentRead=True).get("Item")
    return None if item is None else Session.from_item(item)


def a_launch(written_at: int) -> None:
    """Puts the heartbeat row of an instance of the coursework pool, launched and not yet serving,
    as its lifecycle agent writes it at that time."""
    release = {"image_digest": f"sha256:{'4' * 64}", "model_revision": "5" * 40}
    row = InstanceHeartbeat(
        instance="instance-0123456789abcdef",
        pool="coursework",
        running=0,
        waiting=0,
        stage="weights",
        written_at=written_at,
        **release,
    )
    boto3.resource("dynamodb", region_name=REGION).Table(HEARTBEATS).put_item(Item=row.to_item())


class Stacks:
    """A CloudFormation client as far as the script uses one: it names the pool-state table by its
    logical ID in the pools stack."""

    def describe_stack_resource(self, **request: str) -> dict[str, Any]:
        assert request == {"StackName": POOLS_STACK, "LogicalResourceId": POOL_STATE_LOGICAL_ID}
        return {"StackResourceDetail": {"PhysicalResourceId": POOL_STATE_TABLE}}


class Clock:
    def __init__(self) -> None:
        self.now = float(NOW)

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class SmokeRun:
    """In place of the run of the smoke tests: it keeps what each run was given, the coursework
    pool's session as the table holds it while the tests run and when they started, leaves
    `junit_xml` as the run's record when one is given, takes five minutes and ends in `exit_code`."""

    def __init__(self, pool_state: Any, clock: Clock, junit_xml: str | None = ALL_PASSED, exit_code: int = 0) -> None:
        self.pool_state, self.clock, self.junit_xml, self.exit_code = pool_state, clock, junit_xml, exit_code
        self.given: list[tuple[dict[str, str], list[str]]] = []
        self.sessions: list[Session | None] = []
        self.started_at: list[float] = []

    def __call__(self, env: Mapping[str, str], arguments: Sequence[str]) -> int:
        self.given.append((dict(env), list(arguments)))
        self.sessions.append(session_of(self.pool_state))
        self.started_at.append(self.clock.now)
        (report,) = [
            argument.removeprefix("--junitxml=") for argument in arguments if argument.startswith("--junitxml=")
        ]
        if self.junit_xml is not None:
            Path(report).write_text(self.junit_xml, encoding="utf-8")
        self.clock.sleep(300)
        return self.exit_code


def ready_after(clock: Clock, seconds: float) -> Callable[[str, float], str | None]:
    """A status that reads the coursework pool warming until that long after the job began, then ready."""

    def read(pool: str, _within: float) -> str | None:
        assert pool == "coursework"
        return "ready" if clock.now >= NOW + seconds else "warming"

    return read


def never_ready(pool: str, _within: float) -> str | None:
    return "warming"


def canary(
    clock: Clock, run: Any, status: Callable[[str, float], str | None], /, **changes: Any
) -> tuple[int, dict[str, Any], str]:
    """`canary_result.main` as the job runs it, with moto's tables behind the stand-ins for AWS and
    a clock that only its `sleep` moves: its exit, the detail of the one event it sent, and the key
    the tests were to be given."""
    key, sent = "a-key-drawn-for-this-test", []
    stand_ins: dict[str, Any] = {
        "clock": clock.time,
        "sleep": clock.sleep,
        "secrets": Secrets(key),
        "ssm": Parameters(),
        "events": Events(sent),
        "dynamodb": boto3.client("dynamodb", region_name=REGION),
        "cloudformation": Stacks(),
        "resource": boto3.resource("dynamodb", region_name=REGION),
        "status": status,
        "run": run,
    }
    exit_code = canary_result.main(changes.pop("environ", environ()), **{**stand_ins, **changes})
    (entry,) = sent
    assert key not in entry["Detail"]
    return exit_code, detail(entry), key


def test_the_tests_ran_and_passed_in_a_session_the_canary_booked_and_stopped(pool_state: Any) -> None:
    clock = Clock()
    tests = SmokeRun(pool_state, clock)

    exit_code, found, key = canary(clock, tests, ready_after(clock, 300))

    assert exit_code == 0
    assert (found["measurement"], found["verdict"], found["label"], found["commit"]) == (
        "gpu-canary",
        "pass",
        "2026-11-22",
        COMMIT,
    )
    # From the booking to the first reading of ready.
    assert found["numbers"] == {
        "wait_seconds": 300.0,
        "smoke_tests_passed": 3,
        "smoke_tests_failed": 0,
        "no_capacity": 0,
    }
    # While the tests ran, the pool held the canary's booking: one instance, from the job's
    # beginning, for twice the wait, under the canary's name. They started once the pool read ready.
    (booked,) = tests.sessions
    assert booked is not None
    assert (booked.instances, booked.set_by) == (1, "pipeline/canary")
    assert (booked.starts_at, booked.ends_at) == (NOW, NOW + 2 * WAIT_SECONDS)
    assert tests.started_at == [NOW + 300]
    # Once they ended, five minutes later, the session is stopped: the pool has no active session.
    stopped = session_of(pool_state)
    assert stopped is not None and stopped.ends_at == NOW + 600
    # The tests were given the coursework pool, the key, and the stage's workspace and heartbeats
    # table from the release parameter's settings.
    ((env, arguments),) = tests.given
    assert (env["RHI_SMOKE_POOL"], env["RHI_SMOKE_KEY"]) == ("coursework", key)
    assert env["RHI_SMOKE_WORKSPACE_URL"] == WORKSPACE
    assert env["RHI_VERIFY_HEARTBEATS_TABLE"] == HEARTBEATS
    assert json.loads(env["RHI_SMOKE_RELEASE_PARAMETERS"]) == {"coursework": PARAMETER}
    assert arguments[-1] == "tests/smoke"


def test_a_failed_test_gives_fail_and_the_session_is_stopped_all_the_same(pool_state: Any) -> None:
    clock = Clock()
    tests = SmokeRun(pool_state, clock, ONE_FAILED, exit_code=1)

    exit_code, found, _ = canary(clock, tests, ready_after(clock, 300))

    assert exit_code == 1
    assert found["verdict"] == "fail"
    assert found["numbers"] == {
        "wait_seconds": 300.0,
        "smoke_tests_passed": 2,
        "smoke_tests_failed": 1,
        "no_capacity": 0,
    }
    (booked,) = tests.sessions
    stopped = session_of(pool_state)
    assert booked is not None and stopped is not None
    assert booked.ends_at == NOW + 2 * WAIT_SECONDS and stopped.ends_at == NOW + 600


@pytest.mark.parametrize(
    ("junit_xml", "tests_exit", "counted"),
    [
        pytest.param(ONE_FAILED, 1, {"smoke_tests_passed": 2, "smoke_tests_failed": 1}, id="the tests failed"),
        # The verdict does not rest on the tests: a pool that served only after the wait still
        # did not serve in it.
        pytest.param(ALL_PASSED, 0, {"smoke_tests_passed": 3, "smoke_tests_failed": 0}, id="the tests passed"),
        # A failed canary has both counts, also when the run of the tests recorded none: it left
        # no record, or was killed while it wrote one.
        pytest.param(None, 3, {"smoke_tests_passed": 0, "smoke_tests_failed": 0}, id="the tests left no result"),
        pytest.param("", -15, {"smoke_tests_passed": 0, "smoke_tests_failed": 0}, id="the record cannot be read"),
    ],
)
def test_an_instance_launched_that_never_served_gives_fail(
    pool_state: Any, junit_xml: str | None, tests_exit: int, counted: dict[str, int]
) -> None:
    clock = Clock()
    tests = SmokeRun(pool_state, clock, junit_xml, exit_code=tests_exit)
    # An instance wrote its heartbeat row a minute after the booking, and the pool never read ready.
    a_launch(NOW + 60)

    exit_code, found, _ = canary(clock, tests, never_ready)

    assert exit_code != 0
    assert found["verdict"] == "fail"
    # The whole wait, and not a lack of capacity.
    assert found["numbers"] == {"wait_seconds": float(WAIT_SECONDS), **counted, "no_capacity": 0}
    # The tests ran when the wait ended, and the session is stopped after them.
    assert tests.started_at == [NOW + WAIT_SECONDS]
    stopped = session_of(pool_state)
    assert stopped is not None and stopped.ends_at == NOW + WAIT_SECONDS + 300


def test_no_instance_launched_during_the_wait_is_not_measured_for_lack_of_capacity(pool_state: Any) -> None:
    clock = Clock()
    tests = SmokeRun(pool_state, clock)
    # A row of an instance the pool had before the booking says nothing of a launch since.
    a_launch(NOW - 1)

    exit_code, found, _ = canary(clock, tests, never_ready)

    # A week without capacity is a run that did not measure, not a failed canary.
    assert exit_code == 0
    assert found["verdict"] == "error"
    assert found["numbers"] == {"wait_seconds": float(WAIT_SECONDS), "no_capacity": 1}
    # No test ran, and the session the canary booked is stopped when the wait ends.
    assert tests.given == []
    stopped = session_of(pool_state)
    assert stopped is not None and (stopped.set_by, stopped.ends_at) == ("pipeline/canary", NOW + WAIT_SECONDS)


@pytest.mark.parametrize(
    ("junit_xml", "tests_exit", "verdict"),
    [pytest.param(ALL_PASSED, 0, "pass", id="passing"), pytest.param(ONE_FAILED, 1, "fail", id="failing")],
)
def test_a_pool_that_already_has_a_session_is_neither_booked_nor_stopped(
    pool_state: Any, junit_xml: str, tests_exit: int, verdict: str
) -> None:
    # Someone booked two instances an hour ago, for three hours.
    theirs = Session(
        pool="coursework", instances=2, starts_at=NOW - 3600, ends_at=NOW + 7200, set_by="owner", set_at=NOW - 3600
    )
    pool_state.put_item(Item=theirs.to_item())
    clock = Clock()
    tests = SmokeRun(pool_state, clock, junit_xml, exit_code=tests_exit)

    exit_code, found, _ = canary(clock, tests, never_ready)

    # The tests ran at once, on the instances that are there, with no wait of the canary's.
    assert (exit_code, found["verdict"]) == (tests_exit, verdict)
    assert (found["numbers"]["wait_seconds"], found["numbers"]["no_capacity"]) == (0.0, 0)
    assert tests.started_at == [NOW]
    # The session is theirs while the tests run and after them, as it was.
    assert tests.sessions == [theirs] and session_of(pool_state) == theirs


def test_a_pool_that_was_ready_when_the_job_began_has_no_wait_also_when_the_canary_books_it(pool_state: Any) -> None:
    clock = Clock()
    tests = SmokeRun(pool_state, clock)

    def slow_but_ready(pool: str, _within: float) -> str | None:
        # Each reading takes a moment: the booking's own first reading comes later than the job's.
        clock.sleep(3)
        return "ready"

    exit_code, found, _ = canary(clock, tests, slow_but_ready)

    assert (exit_code, found["verdict"]) == (0, "pass")
    assert found["numbers"]["wait_seconds"] == 0.0
    # The pool had no session, so the canary booked it, and stopped its booking.
    (booked,) = tests.sessions
    stopped = session_of(pool_state)
    assert booked is not None and booked.set_by == "pipeline/canary"
    assert stopped is not None and stopped.ends_at < booked.ends_at


@pytest.mark.parametrize(
    ("endpoint", "held"), [pytest.param("none", "a-key", id="no listener"), pytest.param("canary.example.com", None)]
)
def test_no_test_run_is_sent_as_not_measured_and_books_nothing(
    pool_state: Any, endpoint: str, held: str | None
) -> None:
    def run(_env: Mapping[str, str], _arguments: Sequence[str]) -> int:
        raise AssertionError("no test runs")

    exit_code, found, _ = canary(
        Clock(), run, never_ready, environ=environ() | {"RHI_SMOKE_ENDPOINT": endpoint}, secrets=Secrets(held)
    )
    assert exit_code == 0
    # Not measured, and not for lack of capacity.
    assert (found["verdict"], found["numbers"]) == ("error", {"no_capacity": 0})
    assert session_of(pool_state) is None


def test_without_what_the_booking_needs_no_test_runs_and_the_job_fails(pool_state: Any) -> None:
    tests = SmokeRun(pool_state, Clock())
    given = environ()
    del given["RHI_VERIFY_CHECK_WAIT_MINUTES"]
    exit_code, found, _ = canary(Clock(), tests, never_ready, environ=given)
    assert exit_code == 1
    assert (found["verdict"], found["numbers"]) == ("error", {"no_capacity": 0})
    assert tests.given == [] and session_of(pool_state) is None


def test_a_run_pytest_did_not_finish_is_not_a_pass(pool_state: Any) -> None:
    partial = canary_result.read_results(INTERRUPTED)
    # The unfinished case counts as neither passed nor failed.
    assert (partial.passed, partial.failed, partial.finished) == (1, 0, False)
    # Whatever the XML holds, a run pytest ended as interrupted (2) did not end.
    clock = Clock()
    exit_code, found, _ = canary(clock, SmokeRun(pool_state, clock, ALL_PASSED, exit_code=2), ready_after(clock, 0))
    assert (exit_code, found["verdict"]) == (2, "error")
    assert (found["numbers"]["smoke_tests_passed"], found["numbers"]["no_capacity"]) == (3, 0)


def test_a_run_of_the_tests_that_exits_0_and_is_not_green_fails_the_build(pool_state: Any) -> None:
    # The run left no result: no test is known to have ended.
    clock = Clock()
    exit_code, found, _ = canary(clock, SmokeRun(pool_state, clock, None, exit_code=0), ready_after(clock, 0))
    assert (exit_code, found["verdict"]) == (1, "error")
    assert found["numbers"] == {"wait_seconds": 0.0, "no_capacity": 0}


def test_a_record_of_the_tests_that_cannot_be_read_is_sent_as_not_measured(pool_state: Any) -> None:
    # The run was killed while it wrote its record, on a pool that served: nothing is known of a test.
    clock = Clock()
    exit_code, found, _ = canary(clock, SmokeRun(pool_state, clock, "<testsuites>", exit_code=0), ready_after(clock, 0))
    assert (exit_code, found["verdict"]) == (1, "error")
    assert found["numbers"] == {"wait_seconds": 0.0, "no_capacity": 0}
    # The session the canary booked is stopped all the same.
    stopped = session_of(pool_state)
    assert stopped is not None and stopped.ends_at == NOW + 300


def test_an_event_the_bus_refuses_fails_the_script() -> None:
    with pytest.raises(RuntimeError, match="did not take"):
        canary_result.main(
            environ() | {"RHI_SMOKE_ENDPOINT": "none"}, clock=SUNDAY.timestamp, events=Events([], failed=True)
        )


def collected(env: Mapping[str, str], arguments: Sequence[str]) -> set[str]:
    """The smoke tests a run of the verification's runner would make, by their node IDs, with the
    repository's package roots on the import path, as the canary's job has them."""
    command = [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "verify", *arguments]
    roots = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    folders = os.pathsep.join(["scripts/pipeline", *roots["pythonpath"], *filter(None, [os.environ.get("PYTHONPATH")])])
    answer = subprocess.run(
        command, cwd=REPO, env={**os.environ, **env, "PYTHONPATH": folders}, capture_output=True, text=True, check=True
    )
    return {line for line in answer.stdout.splitlines() if line.startswith("tests/smoke/") and "::" in line}


def test_the_run_makes_every_test_of_the_verification_on_the_coursework_pool(pool_state: Any) -> None:
    clock = Clock()
    tests = SmokeRun(pool_state, clock, None, exit_code=5)
    canary(clock, tests, ready_after(clock, 0))
    ((env, given),) = tests.given
    arguments = [argument for argument in given if not argument.startswith("--junitxml=")]
    endpoint = {"RHI_SMOKE_ENDPOINT": env["RHI_SMOKE_ENDPOINT"], "RHI_SMOKE_POOL": env["RHI_SMOKE_POOL"]}
    after_a_refresh = collected(endpoint | {"RHI_SMOKE_POOL": "coursework"}, ["tests/smoke"])
    assert any("release_heartbeats" in test for test in after_a_refresh)
    assert any("gpu_release" in test for test in after_a_refresh)
    # Every test of the verification's run after a refresh, the release served and the heartbeat
    # rows among them, and no other.
    assert collected(endpoint, arguments) == after_a_refresh
    assert verify.SMOKE_TESTS in arguments
