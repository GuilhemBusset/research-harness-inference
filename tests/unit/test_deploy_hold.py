"""The deployment hold (scripts/pipeline/deploy_hold.py): it passes once the session switch reads
off, at once when it already does, when its item is missing, or when the pools stack or its table
does not exist yet; while it reads on, it reads it again every 60 seconds.

Stand-ins answer in the place of CloudFormation and DynamoDB, and a clock the test controls stands
for time: its `sleep` moves it forward.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

import deploy_hold
from rhi_common.records import SessionSwitch
from rhi_common.tables import SESSION_SWITCH_KEY

STACK = "Rhi-Pools"
LOGICAL_ID = "TablesPoolState68A872F7"
TABLE = "Rhi-Pools-TablesPoolState68A872F7-EXAMPLE"
ENVIRON = {deploy_hold.STACK_VARIABLE: STACK, deploy_hold.LOGICAL_ID_VARIABLE: LOGICAL_ID}


def error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Read")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class CloudFormation:
    """Names the table by its logical ID in the stack, or fails with `failure`."""

    def __init__(self, failure: ClientError | None = None) -> None:
        self.failure = failure
        self.reads = 0

    def describe_stack_resource(self, *, StackName: str, LogicalResourceId: str) -> dict[str, Any]:
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        assert (StackName, LogicalResourceId) == (STACK, LOGICAL_ID)
        return {"StackResourceDetail": {"PhysicalResourceId": TABLE}}


class DynamoDB:
    """The pool-state table, whose switch reads each of `answers` in turn: on, off, no item, or the
    item given."""

    def __init__(self, *answers: bool | dict[str, Any] | None, failure: ClientError | None = None) -> None:
        self.answers = list(answers)
        self.failure = failure
        self.reads = 0

    def Table(self, name: str) -> DynamoDB:
        assert name == TABLE
        return self

    def get_item(self, *, Key: dict[str, str], ConsistentRead: bool) -> dict[str, Any]:
        assert Key == SESSION_SWITCH_KEY and ConsistentRead
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        on = self.answers.pop(0)
        if isinstance(on, dict):
            return {"Item": on}
        return {} if on is None else {"Item": SessionSwitch(on=on, set_by="admin", set_at=1).to_item()}


def run(cloudformation: CloudFormation, dynamodb: DynamoDB, clock: Clock) -> int:
    return deploy_hold.main(ENVIRON, cloudformation=cloudformation, dynamodb=dynamodb, sleep=clock.sleep)


@pytest.mark.parametrize("answer", [pytest.param(False, id="switch off"), pytest.param(None, id="item missing")])
def test_passes_at_once_when_no_session_is_on(answer: bool | None) -> None:
    dynamodb, clock = DynamoDB(answer), Clock()
    assert run(CloudFormation(), dynamodb, clock) == 0
    assert (dynamodb.reads, clock.now) == (1, 0)


@pytest.mark.parametrize(
    "message", ["Stack with id Rhi-Pools does not exist", "Resource TablesPoolState68A872F7 does not exist for stack"]
)
def test_passes_at_once_when_the_stack_or_its_table_does_not_exist(message: str) -> None:
    cloudformation, dynamodb, clock = CloudFormation(error("ValidationError", message)), DynamoDB(), Clock()
    assert run(cloudformation, dynamodb, clock) == 0
    assert (cloudformation.reads, dynamodb.reads, clock.now) == (1, 0, 0)


def test_passes_at_once_when_the_table_is_not_there() -> None:
    dynamodb, clock = DynamoDB(failure=error("ResourceNotFoundException")), Clock()
    assert run(CloudFormation(), dynamodb, clock) == 0
    assert (dynamodb.reads, clock.now) == (1, 0)


def test_waits_while_the_session_is_on() -> None:
    dynamodb, clock = DynamoDB(True, True, True, False), Clock()
    assert run(CloudFormation(), dynamodb, clock) == 0
    assert (dynamodb.reads, clock.now) == (4, 180)


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


def test_a_switch_that_does_not_read_holds_the_deployment() -> None:
    dynamodb = DynamoDB({**SESSION_SWITCH_KEY, "on": "yes", "set_by": "admin", "set_at": 1})
    assert run(CloudFormation(), dynamodb, Clock()) == 1
