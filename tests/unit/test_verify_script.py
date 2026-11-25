"""The verification script (scripts/pipeline/verify.py): with no listener or no canary key it runs
no test, exits 0 and leaves its reason; with a key it runs the smoke tests, on the mock pool all but
those of the coursework pool's run, a failed test is a non-zero exit and leaves its ID, and
what the tests print reaches the log without the key. After a failed run it leaves the last
lifecycle stage each instance of the tested pools reached, and nothing else of a heartbeat's row.
When the decision on the coursework pool left that pool, the smoke tests run again on it, with the
tests of that run only.

Around its run on the mock pool the script books that pool, waits until the gateway's status reads
it ready or the wait is over, and stops the session it booked, also when the tests fail; a pool
that already has a session is neither booked nor stopped.

A stand-in for the Secrets Manager client replaces AWS, and moto holds the heartbeats table and
the pool-state table in process: no account, no credentials, no network. A clock the test controls
stands for time: its `sleep` moves it forward. The tests that pytest really collects and runs do
so in a process of their own, as the script starts one, and reach no stack.
"""

from __future__ import annotations

import json
import os
import runpy
import secrets
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import verify
from rhi_common.records import SESSION_RECORD, GatewayHeartbeat, InstanceHeartbeat, Session
from rhi_common.tables import HEARTBEATS, POOL_STATE

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

REPO = Path(__file__).resolve().parents[2]
SMOKE_TEST_IDS: tuple[str, ...] = runpy.run_path(str(REPO / "tests/smoke/ids.py"))["SMOKE_TEST_IDS"]
ENDPOINT = "smoke.example.com"


class Secrets:
    """A Secrets Manager client as far as the script uses one: the canary's secret holds `held`,
    does not exist when `held` is None, or cannot be read, with the error code `refused`."""

    def __init__(self, held: str | None, *, refused: str | None = None) -> None:
        self.held, self.refused, self.asked = held, refused, []

    def get_secret_value(self, **request: Any) -> dict[str, Any]:
        self.asked.append(request)
        code = self.refused or ("ResourceNotFoundException" if self.held is None else None)
        if code is not None:
            raise ClientError({"Error": {"Code": code, "Message": "refused"}}, "GetSecretValue")
        return {"Name": request["SecretId"], "SecretString": self.held}


class Run:
    """In place of the run of the smoke tests: records the environment it was given."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.environments: list[Mapping[str, str]] = []

    def __call__(self, environ: Mapping[str, str]) -> int:
        self.environments.append(environ)
        return self.exit_code


def a_key() -> str:
    """A key drawn now: none is written in the repository."""
    from rhi_common.keys import generate_key

    return generate_key()


REGION = "us-east-1"
POOLS_STACK = "Rhi-Pools"
POOL_STATE_LOGICAL_ID = "TablesPoolState68A872F7"
POOL_STATE_TABLE = "Rhi-Pools-TablesPoolState68A872F7-EXAMPLE"
NOW = 1_800_000_000
# What the step gives for the session on the mock pool: a wait of 45 minutes, a session of 12 hours at most.
BOOKING = {
    verify.POOLS_STACK_VARIABLE: POOLS_STACK,
    verify.POOL_STATE_LOGICAL_ID_VARIABLE: POOL_STATE_LOGICAL_ID,
    verify.CHECK_WAIT_MINUTES_VARIABLE: "45",
    verify.SESSION_MAX_HOURS_VARIABLE: "12",
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
    """The stage's pool-state table, in moto, with no item: every test of the script has one."""
    with mock_aws():
        create_table(POOL_STATE_TABLE, POOL_STATE)
        yield boto3.resource("dynamodb", region_name=REGION).Table(POOL_STATE_TABLE)


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


def verification(environ: Mapping[str, str], clock: Clock | None = None, **given: Any) -> int:
    """`verify.main` in `environ` and the booking's variables, with moto's pool-state table behind
    the stand-in for CloudFormation, a status that reads the mock pool ready unless one is given,
    and a clock that only its `sleep` moves."""
    clock = clock or Clock()
    stand_ins: dict[str, Any] = {
        "status": lambda: "ready",
        "cloudformation": Stacks(),
        "resource": boto3.resource("dynamodb", region_name=REGION),
    }
    return verify.main({**BOOKING, **environ}, sleep=clock.sleep, clock=clock.time, **{**stand_ins, **given})


@pytest.mark.parametrize("held", [None, "", " \n"], ids=["no secret", "an empty secret", "a blank secret"])
def test_with_no_canary_key_no_test_runs_and_the_reason_is_canary_missing(tmp_path: Path, held: str | None) -> None:
    reason, run, secrets = tmp_path / "build" / "not-run", Run(), Secrets(held)
    environ = {verify.ENDPOINT_VARIABLE: ENDPOINT, verify.REASON_FILE_VARIABLE: str(reason)}

    assert verify.main(environ, client=secrets, run=run) == 0

    assert run.environments == []
    assert reason.read_text(encoding="utf-8") == "canary-missing\n"
    assert secrets.asked == [{"SecretId": "rhi/canary-key"}]


@pytest.mark.parametrize(
    ("endpoint", "held"), [("none", "a-key"), (ENDPOINT, None)], ids=["no listener", "no canary key"]
)
def test_when_no_test_runs_the_mock_pool_is_not_booked(
    tmp_path: Path, pool_state: Any, endpoint: str, held: str | None
) -> None:
    environ = {verify.ENDPOINT_VARIABLE: endpoint, verify.REASON_FILE_VARIABLE: str(tmp_path / "not-run")}
    assert verification(environ, client=Secrets(held), run=Run()) == 0
    assert pool_state.scan()["Items"] == []


def test_with_no_listener_no_test_runs_and_the_reason_is_listener_off(tmp_path: Path) -> None:
    reason, run, secrets = tmp_path / "not-run", Run(), Secrets(a_key())
    environ = {verify.ENDPOINT_VARIABLE: "none", verify.REASON_FILE_VARIABLE: str(reason)}

    assert verify.main(environ, client=secrets, run=run) == 0

    assert run.environments == []
    assert reason.read_text(encoding="utf-8") == "listener-off\n"
    # There is nothing to call: the key is not even read.
    assert secrets.asked == []


def test_a_secret_that_cannot_be_read_is_not_a_missing_canary(tmp_path: Path) -> None:
    reason, run = tmp_path / "not-run", Run()
    environ = {verify.ENDPOINT_VARIABLE: ENDPOINT, verify.REASON_FILE_VARIABLE: str(reason)}

    with pytest.raises(ClientError):
        verify.main(environ, client=Secrets(a_key(), refused="AccessDeniedException"), run=run)

    assert run.environments == [] and not reason.exists()


def test_no_endpoint_given_is_an_error(tmp_path: Path) -> None:
    run = Run()
    assert verify.main({verify.REASON_FILE_VARIABLE: str(tmp_path / "not-run")}, client=Secrets(a_key()), run=run) != 0
    assert run.environments == []


@pytest.mark.parametrize("exit_code", [0, 1, 5], ids=["every test passed", "a test failed", "no test was collected"])
def test_with_a_key_the_tests_run_with_it_and_their_exit_is_the_script_s(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], exit_code: int
) -> None:
    key, reason, run = a_key(), tmp_path / "not-run", Run(exit_code)
    environ = {
        verify.ENDPOINT_VARIABLE: ENDPOINT,
        verify.REASON_FILE_VARIABLE: str(reason),
        verify.RELEASE_PARAMETERS_VARIABLE: "mock=/rhi/pools/mock/release,other-1=/rhi/pools/other-1/release",
        "RHI_SMOKE_WORKSPACE_URL": "https://workspace.example.com/",
    }

    assert verification(environ, client=Secrets(f"{key}\n"), run=run) == exit_code

    (given,) = run.environments
    # What the smoke tests read: the endpoint and the workspace as given, the key, and the pools'
    # parameters as JSON.
    assert given[verify.ENDPOINT_VARIABLE] == ENDPOINT
    assert given["RHI_SMOKE_WORKSPACE_URL"] == "https://workspace.example.com/"
    assert given["RHI_SMOKE_KEY"] == key
    assert json.loads(given["RHI_SMOKE_RELEASE_PARAMETERS"]) == {
        "mock": "/rhi/pools/mock/release",
        "other-1": "/rhi/pools/other-1/release",
    }
    assert not reason.exists()
    assert key not in "".join(capsys.readouterr())


def coursework_pass(tmp_path: Path, left: str | None, exits: list[int]) -> tuple[int, list[Mapping[str, str]]]:
    """The script's exit after runs of the tests that end in `exits`, one after the other, once the
    decision left `left` as the pools to verify on, None for no file, and the environments the runs
    were given."""
    named, given = tmp_path / "gpu-pools", []
    if left is not None:
        named.write_text(left, encoding="utf-8")

    def run(environ: Mapping[str, str]) -> int:
        given.append(environ)
        return exits[len(given) - 1]

    environ = {
        verify.ENDPOINT_VARIABLE: ENDPOINT,
        verify.RELEASE_PARAMETERS_VARIABLE: "mock=/rhi/pools/mock/release,coursework=/rhi/pools/coursework/release",
        verify.GPU_POOLS_FILE_VARIABLE: str(named),
    }
    return verification(environ, client=Secrets(a_key()), run=run), given


def test_the_pools_the_decision_left_are_run_on_after_the_mock_pool(tmp_path: Path) -> None:
    exit_code, given = coursework_pass(tmp_path, "coursework\n", [0, 0])
    assert exit_code == 0
    # The mock pool's run names no pool; the coursework pool's names it, with the same key and pools.
    assert [environ.get(verify.POOL_VARIABLE) for environ in given] == [None, "coursework"]
    assert given[0][verify.KEY_VARIABLE] == given[1][verify.KEY_VARIABLE]
    # Every run is made, and the exit is the first failed one's.
    assert coursework_pass(tmp_path, "coursework\n", [0, 3])[0] == 3
    exit_code, given = coursework_pass(tmp_path, "coursework\n", [2, 0])
    assert exit_code == 2 and len(given) == 2


def test_no_pool_left_by_the_decision_is_the_mock_pool_s_run_alone(tmp_path: Path) -> None:
    exit_code, given = coursework_pass(tmp_path, "", [0])
    assert exit_code == 0 and [environ.get(verify.POOL_VARIABLE) for environ in given] == [None]


def test_a_decision_that_left_no_file_runs_no_test(tmp_path: Path) -> None:
    assert coursework_pass(tmp_path, None, []) == (1, [])


def test_no_pool_is_no_release_parameter() -> None:
    assert verify.release_parameters("none") == {}


def session_of(pool_state: Any) -> Session | None:
    """The mock pool's session as the table holds it, None for no item."""
    item = pool_state.get_item(Key={"pool": "mock", "record": SESSION_RECORD}, ConsistentRead=True).get("Item")
    return None if item is None else Session.from_item(item)


class Booked:
    """In place of the run of the smoke tests: keeps the mock pool's session as the table holds it
    while the tests run, and when they started; the tests take five minutes and end in `exit_code`."""

    def __init__(self, pool_state: Any, clock: Clock, exit_code: int = 0) -> None:
        self.pool_state, self.clock, self.exit_code = pool_state, clock, exit_code
        self.sessions: list[Session | None] = []
        self.started_at: list[float] = []

    def __call__(self, environ: Mapping[str, str]) -> int:
        self.sessions.append(session_of(self.pool_state))
        self.started_at.append(self.clock.now)
        self.clock.sleep(300)
        return self.exit_code


ENVIRON = {verify.ENDPOINT_VARIABLE: ENDPOINT}


@pytest.mark.parametrize("exit_code", [0, 1], ids=["the tests pass", "a test fails"])
def test_the_mock_pool_is_booked_before_the_tests_and_stopped_after_them(pool_state: Any, exit_code: int) -> None:
    clock = Clock()
    run = Booked(pool_state, clock, exit_code)

    assert verification(ENVIRON, clock, client=Secrets(a_key()), run=run) == exit_code

    # While the tests ran: one instance, under the step's name, for twice the wait, 90 minutes.
    booked = Session(pool="mock", instances=1, starts_at=NOW, ends_at=NOW + 5400, set_by="pipeline/verify", set_at=NOW)
    assert run.sessions == [booked]
    # After them, whatever their result: the same session, ended when the tests did.
    ended = NOW + 300
    assert session_of(pool_state) == Session(
        pool="mock", instances=1, starts_at=NOW, ends_at=ended, set_by="pipeline/verify", set_at=ended
    )


def test_the_session_is_stopped_before_the_coursework_pool_s_run_and_when_the_run_breaks(
    tmp_path: Path, pool_state: Any
) -> None:
    named = tmp_path / "gpu-pools"
    named.write_text("coursework\n", encoding="utf-8")
    clock = Clock()
    run = Booked(pool_state, clock)

    assert (
        verification({**ENVIRON, verify.GPU_POOLS_FILE_VARIABLE: str(named)}, clock, client=Secrets(a_key()), run=run)
        == 0
    )

    # The mock pool's run is in the session; the coursework pool's comes after its end.
    on_mock, on_coursework = run.sessions
    assert on_mock is not None and on_coursework is not None
    assert on_mock.ends_at == NOW + 5400 and on_coursework.ends_at == NOW + 300

    def broken(environ: Mapping[str, str]) -> int:
        raise RuntimeError("the run of the tests broke")

    later = Clock()
    later.now = NOW + 3600
    with pytest.raises(RuntimeError):
        verification(ENVIRON, later, client=Secrets(a_key()), run=broken)
    stopped = session_of(pool_state)
    assert stopped is not None and stopped.starts_at == stopped.ends_at == NOW + 3600


@pytest.mark.parametrize(
    ("starts_in", "ends_in"), [(600, 4200), (-600, 600)], ids=["a session booked", "a session active"]
)
def test_a_pool_that_already_has_a_session_is_neither_booked_nor_stopped(
    pool_state: Any, starts_in: int, ends_in: int
) -> None:
    theirs = Session(
        pool="mock", instances=2, starts_at=NOW + starts_in, ends_at=NOW + ends_in, set_by="admin", set_at=NOW - 900
    )
    pool_state.put_item(Item=theirs.to_item())
    clock = Clock()
    run = Booked(pool_state, clock)

    def never_read() -> str:
        raise AssertionError("the status is not waited for on a pool the step did not book")

    assert verification(ENVIRON, clock, client=Secrets(a_key()), run=run, status=never_read) == 0

    # The tests ran at once, on the session that was there, which is as it was after them.
    assert run.sessions == [theirs] and run.started_at == [NOW]
    assert session_of(pool_state) == theirs


def test_a_session_someone_else_booked_meanwhile_is_not_stopped(pool_state: Any) -> None:
    clock = Clock()
    theirs = Session(pool="mock", instances=2, starts_at=NOW + 60, ends_at=NOW + 7200, set_by="admin", set_at=NOW + 60)

    def run(environ: Mapping[str, str]) -> int:
        # During the tests, an admin stops the step's session and books the pool.
        pool_state.put_item(Item=theirs.to_item())
        return 0

    assert verification(ENVIRON, clock, client=Secrets(a_key()), run=run) == 0
    assert session_of(pool_state) == theirs


def states(*read: str | None) -> Callable[[], str | None]:
    """A status that reads each of `read` in turn, then the last of them for good."""
    left = list(read)
    return lambda: left.pop(0) if len(left) > 1 else left[0]


@pytest.mark.parametrize(
    ("status", "waited"),
    [
        (states("ready"), 0),
        (states("cold", "warming", None, "ready"), 3 * verify.POLL_SECONDS),
        (states("warming"), 45 * 60),
        (states(None), 45 * 60),
    ],
    ids=["ready at once", "ready at the fourth reading", "never ready", "no reading"],
)
def test_the_tests_start_once_the_status_reads_ready_or_the_wait_is_over(
    pool_state: Any, status: Callable[[], str | None], waited: float
) -> None:
    clock = Clock()
    run = Booked(pool_state, clock)

    assert verification(ENVIRON, clock, client=Secrets(a_key()), run=run, status=status) == 0

    # At the first reading that says ready, or 45 minutes after the booking and no later: in the
    # session, which lasts as long again.
    assert run.started_at == [NOW + waited]
    (during,) = run.sessions
    assert during is not None and during.ends_at == NOW + 90 * 60
    stopped = session_of(pool_state)
    assert stopped is not None and stopped.ends_at == NOW + waited + 300


def test_the_pool_s_state_is_read_from_the_gateway_s_status_with_the_canary_s_key() -> None:
    import httpx

    asked: list[httpx.Request] = []
    answers = iter(
        [
            httpx.Response(
                200, json={"pools": [{"model": "coursework", "state": "ready"}, {"model": "mock", "state": "cold"}]}
            ),
            httpx.Response(200, json={"pools": [{"model": "mock", "state": "ready"}]}),
            httpx.Response(200, json={"pools": [{"model": "coursework", "state": "ready"}]}),
            httpx.Response(503, json={"pools": [{"model": "mock", "state": "ready"}]}),
            httpx.Response(200, text="not a status"),
        ]
    )

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return next(answers)

    read = verify.status_reader(ENDPOINT, "a-key", httpx.MockTransport(answer))

    # The mock pool's own state; a status that does not give it, or does not answer, reads as none.
    assert [read() for _ in range(5)] == ["cold", "ready", None, None, None]
    assert {str(request.url) for request in asked} == {f"https://{ENDPOINT}/status"}
    assert {request.headers["Authorization"] for request in asked} == {"Bearer a-key"}


def test_a_pool_that_cannot_be_booked_fails_the_script_before_any_test(
    pool_state: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    class Refuses:
        def describe_stack_resource(self, **_request: str) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "said by AWS"}}, "DescribeStackResource")

    run = Run()
    assert verification(ENVIRON, client=Secrets(a_key()), run=run, cloudformation=Refuses()) != 0
    # Twice the wait is longer than a session may last: nothing is written.
    assert verification({**ENVIRON, verify.SESSION_MAX_HOURS_VARIABLE: "1"}, client=Secrets(a_key()), run=run) != 0
    assert run.environments == [] and session_of(pool_state) is None
    assert "said by AWS" not in "".join(capsys.readouterr())


@pytest.mark.parametrize(
    "given",
    [
        {verify.POOLS_STACK_VARIABLE: ""},
        {verify.POOL_STATE_LOGICAL_ID_VARIABLE: ""},
        {verify.CHECK_WAIT_MINUTES_VARIABLE: "0"},
        {verify.CHECK_WAIT_MINUTES_VARIABLE: "4.5"},
        {verify.SESSION_MAX_HOURS_VARIABLE: "twelve"},
    ],
    ids=["no stack", "no logical ID", "a wait of zero", "a wait that is not whole", "hours that are no number"],
)
def test_without_what_the_booking_needs_no_test_runs(pool_state: Any, given: dict[str, str]) -> None:
    run = Run()
    assert verification({**ENVIRON, **given}, client=Secrets(a_key()), run=run) != 0
    assert run.environments == [] and session_of(pool_state) is None


def test_a_session_that_could_not_be_stopped_fails_a_run_whose_tests_passed(pool_state: Any) -> None:
    class StopsNothing:
        """The pool-state table, whose second write, the stop's, is refused."""

        def __init__(self) -> None:
            self.writes = 0

        def Table(self, name: str) -> StopsNothing:
            assert name == POOL_STATE_TABLE
            return self

        def get_item(self, **request: Any) -> dict[str, Any]:
            return pool_state.get_item(**request)

        def put_item(self, **request: Any) -> dict[str, Any]:
            self.writes += 1
            if self.writes > 1:
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "said by AWS"}}, "PutItem")
            return pool_state.put_item(**request)

    def result(exit_code: int) -> int:
        pool_state.delete_item(Key={"pool": "mock", "record": SESSION_RECORD})
        return verification(ENVIRON, client=Secrets(a_key()), run=Run(exit_code), resource=StopsNothing())

    assert result(0) != 0
    # The exit of tests that failed stays the script's.
    assert result(5) == 5


TABLE = "heartbeats-under-test"


@pytest.fixture
def heartbeats() -> Iterator[Any]:
    """A DynamoDB client, in moto, of a region that holds the heartbeats table with the key schema
    of `rhi_common.tables.HEARTBEATS`, and no row."""
    with mock_aws():
        create_table(TABLE, HEARTBEATS)
        yield boto3.client("dynamodb", region_name=REGION)


def instance_row(pool: str, stage: str) -> tuple[str, dict[str, Any]]:
    """An instance's heartbeat as its lifecycle agent writes it, with the stage it has reached: the
    instance's ID, drawn now, and the row's item."""
    instance = f"instance-{secrets.token_hex(8)}"
    release = {"image_digest": f"sha256:{secrets.token_hex(32)}", "model_revision": secrets.token_hex(20)}
    row = InstanceHeartbeat(instance=instance, pool=pool, running=0, waiting=0, stage=stage, written_at=1, **release)
    return instance, row.to_item()


def failed_run(tmp_path: Path, dynamodb: Any, exit_code: int = 1) -> tuple[int, str | None]:
    """The script's exit after a run of the tests of the pools `mock` and `gpu` that ends in
    `exit_code`, and what it left as the instances' last stages, None for no file."""
    left = tmp_path / "build" / "last-stages"
    environ = {
        verify.ENDPOINT_VARIABLE: ENDPOINT,
        verify.RELEASE_PARAMETERS_VARIABLE: "mock=/rhi/pools/mock/release,gpu=/rhi/pools/gpu/release",
        verify.HEARTBEATS_TABLE_VARIABLE: TABLE,
        verify.LAST_STAGES_FILE_VARIABLE: str(left),
    }
    exited = verification(environ, client=Secrets(a_key()), run=Run(exit_code), dynamodb=dynamodb)
    return exited, left.read_text(encoding="utf-8") if left.exists() else None


def test_a_failed_run_leaves_the_last_stage_of_each_instance_of_the_tested_pools_and_nothing_else(
    tmp_path: Path, heartbeats: Any
) -> None:
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    # Two pools the step tested, with an instance that never served, one that serves and a gateway
    # task's own heartbeat, and a pool it did not test.
    rows = [instance_row("mock", "weights"), instance_row("mock", "serving"), instance_row("gpu", "release-check")]
    untested = instance_row("other", "boot")
    for _, item in [*rows, untested]:
        table.put_item(Item=item)
    task = GatewayHeartbeat(task="task-1", pool="mock", in_flight=1, held=0, last_request_at=None, written_at=1)
    table.put_item(Item=task.to_item())

    exit_code, left = failed_run(tmp_path, heartbeats)

    # The tests' exit is the script's, and each line is a stage: no instance, no release, no pool.
    assert exit_code == 1 and left is not None and left.endswith("\n")
    assert sorted(left.splitlines()) == ["release-check", "serving", "weights"]
    # A table read a page at a time gives the same lines.
    assert sorted((failed_run(tmp_path, OneRowAPage(heartbeats))[1] or "").splitlines()) == sorted(left.splitlines())


class OneRowAPage:
    """A DynamoDB client that answers a query one row at a time, as DynamoDB pages a long answer."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def query(self, **request: Any) -> dict[str, Any]:
        return self.client.query(**request, Limit=1)


def test_a_run_that_passed_or_a_pool_with_no_instance_leaves_no_last_stage(tmp_path: Path, heartbeats: Any) -> None:
    # No instance's row: a failed run has no stage to leave.
    assert failed_run(tmp_path, heartbeats) == (1, None)
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(Item=instance_row("mock", "weights")[1])
    # A run whose tests passed does not read the table.
    assert failed_run(tmp_path, None, exit_code=0) == (0, None)
    assert failed_run(tmp_path, heartbeats) == (1, "weights\n")


def test_a_table_that_cannot_be_read_leaves_the_tests_exit_as_it_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Refuses:
        def query(self, **_request: Any) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "said by AWS"}}, "Query")

    assert failed_run(tmp_path, Refuses(), exit_code=2) == (2, None)
    printed = capsys.readouterr().err
    assert "last stages were not read" in printed and "said by AWS" not in printed


# A plugin of the collection below: it writes, as JSON, the IDs of the tests that were deselected
# and of those that were kept, to the file its variable names.
RECORDER = """
import json, os

deselected = []


def ids(items):
    return [marker.args[0] for item in items for marker in item.iter_markers("smoke_id")]


def pytest_deselected(items):
    deselected.extend(ids(items))


def pytest_collection_finish(session):
    with open(os.environ["RECORDED"], "w", encoding="utf-8") as file:
        json.dump({"deselected": deselected, "kept": ids(session.items)}, file)
"""


def run_tests_in(tmp_path: Path, tests: list[str], *, more: Mapping[str, str] | None = None) -> int:
    """`verify.run_tests` on `tests`, in an environment with an endpoint and a key, where pytest
    also takes what `more` gives it. Nothing of the machine's own smoke variables takes part."""
    environ = {name: value for name, value in os.environ.items() if not name.startswith(("RHI_SMOKE_", "PYTEST_"))}
    environ |= {verify.ENDPOINT_VARIABLE: f"https://{ENDPOINT}", verify.KEY_VARIABLE: a_key(), **(more or {})}
    return verify.run_tests(environ, tests)


@pytest.mark.parametrize("pool", [None, "coursework"], ids=["the mock pool's run", "the coursework pool's run"])
def test_only_the_mock_pool_s_run_deselects_and_only_the_gpu_pass_tests(tmp_path: Path, pool: str | None) -> None:
    (tmp_path / "recorder.py").write_text(RECORDER, encoding="utf-8")
    recorded = tmp_path / "recorded.json"
    # Collection only: nothing is run, so nothing is reached.
    more = {
        "PYTHONPATH": str(tmp_path),
        "PYTEST_ADDOPTS": "-p recorder --collect-only -q -p no:cacheprovider",
        "RECORDED": str(recorded),
        **({verify.POOL_VARIABLE: pool} if pool else {}),
    }

    assert run_tests_in(tmp_path, [verify.SMOKE_TESTS], more=more) == 0

    found = json.loads(recorded.read_text(encoding="utf-8"))
    # The tests of the coursework pool's run are left out of the mock pool's run, and only there.
    left_out = set() if pool else set(verify.GPU_PASS_ONLY)
    assert "gpu-release-served" in verify.GPU_PASS_ONLY
    assert sorted(found["deselected"]) == sorted(left_out)
    # Every other smoke test is kept, the deployed-only ones and the forbidden routes at the
    # gateway's hop among them, and on the coursework pool the first stream's wake and hold.
    assert sorted(found["kept"]) == sorted(set(SMOKE_TEST_IDS) - left_out)
    assert {"forbidden-invocations", "first-stream-wake", "metrics-pool", "release-served"} <= set(found["kept"])


def test_a_failed_test_makes_the_script_exit_non_zero(tmp_path: Path) -> None:
    def run_these(tests: Path) -> int:
        environ = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}
        environ |= {verify.ENDPOINT_VARIABLE: ENDPOINT, verify.REASON_FILE_VARIABLE: str(tmp_path / "not-run")}
        return verification(
            environ,
            client=Secrets(a_key()),
            run=lambda given: verify.run_tests({**given, "PYTEST_ADDOPTS": "-q -p no:cacheprovider"}, [str(tests)]),
        )

    passing, failing = tmp_path / "passing" / "test_it.py", tmp_path / "failing" / "test_it.py"
    for file, body in ((passing, "assert True"), (failing, "assert False")):
        file.parent.mkdir()
        file.write_text(f"def test_it() -> None:\n    {body}\n", encoding="utf-8")

    assert run_these(passing) == 0
    assert run_these(failing) != 0


def test_the_ids_of_the_tests_that_failed_are_left_one_per_line_and_nothing_else(tmp_path: Path) -> None:
    # Tests that carry their IDs as the smoke tests do: one passes, one fails, one fails before it
    # starts, and one is among those the verification leaves out.
    tests = tmp_path / "tests" / "test_it.py"
    tests.parent.mkdir()
    tests.write_text(
        "import pytest\n\n\n"
        "@pytest.fixture\ndef broken() -> None:\n    raise RuntimeError('what a test says is not an ID')\n\n\n"
        "@pytest.mark.smoke_id('hello')\ndef test_passes() -> None:\n    pass\n\n\n"
        "@pytest.mark.smoke_id('no-key')\ndef test_fails() -> None:\n    assert False, 'nor is this'\n\n\n"
        "@pytest.mark.smoke_id('environment')\ndef test_fails_before_it_starts(broken: None) -> None:\n    pass\n\n\n"
        "@pytest.mark.smoke_id('gpu-release-served')\ndef test_is_left_out() -> None:\n    assert False\n",
        encoding="utf-8",
    )
    quiet = "-q -p no:cacheprovider -W ignore::pytest.PytestUnknownMarkWarning"

    def left_by(*selection: str) -> tuple[int, str | None]:
        """The exit of a run of those tests, and what it left as its failed tests, None for no file."""
        left = tmp_path / "build" / "failed"
        left.unlink(missing_ok=True)
        more = {verify.FAILED_TESTS_FILE_VARIABLE: str(left), "PYTEST_ADDOPTS": quiet}
        exit_code = run_tests_in(tmp_path, [str(tests), *selection], more=more)
        return exit_code, left.read_text(encoding="utf-8") if left.exists() else None

    exit_code, left = left_by()
    assert exit_code != 0 and left is not None
    assert sorted(left.splitlines()) == ["environment", "no-key"] and left.endswith("\n")
    # A run in which no test fails leaves no file: there is nothing for the step to copy.
    assert left_by("-k", "passes") == (0, None)
    # A second run of the same verification, on another pool, adds only the IDs not yet left.
    left_by()
    more = {verify.FAILED_TESTS_FILE_VARIABLE: str(tmp_path / "build" / "failed"), "PYTEST_ADDOPTS": quiet}
    assert run_tests_in(tmp_path, [str(tests)], more=more) != 0
    assert sorted((tmp_path / "build" / "failed").read_text(encoding="utf-8").splitlines()) == ["environment", "no-key"]


def test_what_the_tests_print_reaches_the_log_without_the_key(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # A test that fails holding the key, and prints it whole, then cut as pytest cuts a long value.
    key = a_key()
    (tmp_path / "test_it.py").write_text(
        "import os, sys\n\n\n"
        "def test_it() -> None:\n"
        "    key = os.environ['RHI_SMOKE_KEY']\n"
        "    print('whole:', key)\n"
        "    print('cut:', key[:20] + '...' + key[-12:], file=sys.stderr)\n"
        "    print('nothing of a key')\n"
        "    assert key == 'another'\n",
        encoding="utf-8",
    )
    more = {verify.KEY_VARIABLE: key, "PYTEST_ADDOPTS": "-p no:cacheprovider"}

    assert run_tests_in(tmp_path, [str(tmp_path)], more=more) != 0

    printed = "".join(capfd.readouterr())
    # The failure is told, with the lines that hold no key as they were.
    assert "AssertionError" in printed and "nothing of a key" in printed
    assert verify.REDACTED in printed
    assert not [at for at in range(len(key) - verify.PIECE + 1) if key[at : at + verify.PIECE] in printed]


def test_the_script_runs_as_the_step_runs_it(tmp_path: Path) -> None:
    # From the checkout, with no import path given: the script needs none.
    reason = tmp_path / "not-run"
    environ = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    environ |= {verify.ENDPOINT_VARIABLE: "none", verify.REASON_FILE_VARIABLE: str(reason)}
    command = [sys.executable, "scripts/pipeline/verify.py"]
    done = subprocess.run(command, cwd=REPO, env=environ, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert reason.read_text(encoding="utf-8") == "listener-off\n"
