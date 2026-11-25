"""The deployment hold (scripts/pipeline/deploy_hold.py): it passes once no session is active on
a pool of the stage, at once when that is already so, when the items are missing, or when the pools
stack or its table does not exist yet; until then, it reads them again every 60 seconds. It reads
the pools' sessions and no other item.

Stand-ins answer in the place of CloudFormation and DynamoDB, and a clock the test controls stands
for time: its `sleep` moves it forward.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

import deploy_hold
from rhi_common.records import SESSION_RECORD, Session
from rhi_common.sessions import stopped

STACK = "Rhi-Pools"
LOGICAL_ID = "TablesPoolState68A872F7"
TABLE = "Rhi-Pools-TablesPoolState68A872F7-EXAMPLE"
POOLS = ["coursework", "mock"]
ENVIRON = {
    deploy_hold.STACK_VARIABLE: STACK,
    deploy_hold.LOGICAL_ID_VARIABLE: LOGICAL_ID,
    deploy_hold.POOLS_VARIABLE: ",".join(POOLS),
    deploy_hold.SESSION_MAX_HOURS_VARIABLE: "12",
}
HOUR = 3600


def error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Read")


def session(pool: str, starts_at: int, ends_at: int) -> dict[str, Any]:
    """The item of a session of the pool, written at the time 0."""
    booked = Session(pool=pool, instances=1, starts_at=starts_at, ends_at=ends_at, set_by="admin", set_at=0)
    return booked.to_item()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class CloudFormation:
    """Names the table by its logical ID in the stack, or fails with `failure`."""

    def __init__(self, failure: ClientError | None = None, detail: dict[str, Any] | None = None) -> None:
        self.failure = failure
        self.detail = {"PhysicalResourceId": TABLE} if detail is None else detail
        self.reads = 0

    def describe_stack_resource(self, *, StackName: str, LogicalResourceId: str) -> dict[str, Any]:
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        assert (StackName, LogicalResourceId) == (STACK, LOGICAL_ID)
        return {"StackResourceDetail": self.detail}


class DynamoDB:
    """The pool-state table: the item under each pool's session key, for the pools of `sessions`,
    or `failure` on every read. It refuses a read of any other key, the switch an earlier release
    left in the table among them. `asked` lists the pools whose session was read."""

    def __init__(
        self, *, failure: ClientError | None = None, sessions: dict[str, dict[str, Any]] | None = None
    ) -> None:
        self.failure = failure
        self.sessions = sessions or {}
        self.asked: list[str] = []

    def Table(self, name: str) -> DynamoDB:
        assert name == TABLE
        return self

    def get_item(self, *, Key: dict[str, str], ConsistentRead: bool) -> dict[str, Any]:
        assert ConsistentRead
        assert Key == {"pool": Key["pool"], "record": SESSION_RECORD}
        assert Key["pool"] in POOLS
        self.asked.append(Key["pool"])
        if self.failure is not None:
            raise self.failure
        item = self.sessions.get(Key["pool"])
        return {} if item is None else {"Item": item}


def run(cloudformation: CloudFormation, dynamodb: DynamoDB, clock: Clock, **environ: str) -> int:
    return deploy_hold.main(
        ENVIRON | environ, cloudformation=cloudformation, dynamodb=dynamodb, sleep=clock.sleep, clock=clock.time
    )


def test_passes_at_once_when_no_pool_has_a_session() -> None:
    dynamodb, clock = DynamoDB(), Clock()
    assert run(CloudFormation(), dynamodb, clock) == 0
    # No pool has a session item, and every pool was asked for one. The stand-in refuses any other read.
    assert (dynamodb.asked, clock.now) == (POOLS, 0)


@pytest.mark.parametrize(
    "message", ["Stack with id Rhi-Pools does not exist", "Resource TablesPoolState68A872F7 does not exist for stack"]
)
def test_passes_at_once_when_the_stack_or_its_table_does_not_exist(message: str) -> None:
    cloudformation, dynamodb, clock = CloudFormation(error("ValidationError", message)), DynamoDB(), Clock()
    assert run(cloudformation, dynamodb, clock) == 0
    assert (cloudformation.reads, dynamodb.asked, clock.now) == (1, [], 0)


def test_passes_at_once_when_the_table_was_never_created() -> None:
    cloudformation = CloudFormation(detail={"LogicalResourceId": LOGICAL_ID, "ResourceStatus": "CREATE_FAILED"})
    dynamodb, clock = DynamoDB(), Clock()
    assert run(cloudformation, dynamodb, clock) == 0
    assert (cloudformation.reads, dynamodb.asked, clock.now) == (1, [], 0)


def test_passes_at_once_when_the_table_is_not_there() -> None:
    dynamodb, clock = DynamoDB(failure=error("ResourceNotFoundException")), Clock()
    assert run(CloudFormation(), dynamodb, clock) == 0
    assert (dynamodb.asked, clock.now) == (POOLS[:1], 0)


@pytest.mark.parametrize(
    ("cloudformation", "dynamodb"),
    [
        pytest.param(CloudFormation(error("ValidationError", "1 validation error detected")), DynamoDB(), id="lookup"),
        pytest.param(CloudFormation(error("AccessDenied")), DynamoDB(), id="lookup refused"),
        pytest.param(CloudFormation(), DynamoDB(failure=error("AccessDeniedException")), id="read refused"),
    ],
)
def test_a_read_that_fails_otherwise_holds_the_deployment(cloudformation: CloudFormation, dynamodb: DynamoDB) -> None:
    assert run(cloudformation, dynamodb, Clock()) == 1


def test_waits_while_one_pool_s_session_is_active_and_passes_once_it_has_ended() -> None:
    # One pool's session ends at 150 s, and the other's is booked for later.
    sessions = {"mock": session("mock", 0, 150), "coursework": session("coursework", HOUR, 2 * HOUR)}
    dynamodb, clock = DynamoDB(sessions=sessions), Clock()
    assert run(CloudFormation(), dynamodb, clock) == 0
    # Read at 0, 60 and 120 s, when it is active, and at 180 s, when it has ended.
    assert (clock.now, dynamodb.asked) == (180, POOLS * 4)


def test_passes_once_the_session_is_stopped() -> None:
    booked = session("mock", 0, 2 * HOUR)
    dynamodb, clock = DynamoDB(sessions={"mock": booked}), Clock()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.now == 120:
            now = int(clock.now)
            end = stopped(Session.from_item(booked), set_by="admin", now=now, session_max_hours=12)
            assert end is not None
            dynamodb.sessions["mock"] = end.to_item()

    assert deploy_hold.main(ENVIRON, cloudformation=CloudFormation(), dynamodb=dynamodb, sleep=sleep, clock=clock.time) == 0
    assert clock.now == 120


def test_a_session_holds_no_longer_than_the_longest_session() -> None:
    # Its record ends in a day: it is read as ending two hours after it was written.
    dynamodb, clock = DynamoDB(sessions={"mock": session("mock", 0, 24 * HOUR)}), Clock()
    assert run(CloudFormation(), dynamodb, clock, **{deploy_hold.SESSION_MAX_HOURS_VARIABLE: "2"}) == 0
    assert clock.now == 2 * HOUR


@pytest.mark.parametrize(
    "item",
    [
        pytest.param({"pool": "mock", "record": "mode", "mode": "enabled"}, id="another record"),
        pytest.param(session("mock", 0, 150) | {"ends_at": "later"}, id="an end that is not a time"),
    ],
)
def test_an_item_that_is_not_a_session_holds_the_deployment(item: dict[str, Any]) -> None:
    clock = Clock()
    assert run(CloudFormation(), DynamoDB(sessions={"mock": item}), clock) == 1
    assert clock.now == 0


def test_a_stage_with_no_pool_passes_at_once_and_asks_aws_nothing() -> None:
    # A lookup would fail the job: with no pool there is no session to read, so none is made.
    cloudformation, dynamodb, clock = CloudFormation(error("AccessDenied")), DynamoDB(), Clock()
    assert run(cloudformation, dynamodb, clock, **{deploy_hold.POOLS_VARIABLE: ""}) == 0
    assert (cloudformation.reads, dynamodb.asked, clock.now) == (0, [], 0)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        pytest.param(deploy_hold.POOLS_VARIABLE, None, id="no pools named"),
        pytest.param(deploy_hold.POOLS_VARIABLE, "coursework,,mock", id="a pool with no name"),
        pytest.param(deploy_hold.SESSION_MAX_HOURS_VARIABLE, None, id="no hours"),
        pytest.param(deploy_hold.SESSION_MAX_HOURS_VARIABLE, "0", id="zero hours"),
        pytest.param(deploy_hold.SESSION_MAX_HOURS_VARIABLE, "twelve", id="hours that are no number"),
    ],
)
def test_without_its_pools_or_the_longest_session_the_job_fails_before_it_reads(variable: str, value: str | None) -> None:
    environ = {name: text for name, text in ENVIRON.items() if name != variable} | ({} if value is None else {variable: value})
    cloudformation, dynamodb = CloudFormation(), DynamoDB()
    assert deploy_hold.main(environ, cloudformation=cloudformation, dynamodb=dynamodb, sleep=Clock().sleep) == 1
    assert (cloudformation.reads, dynamodb.asked) == (0, [])
