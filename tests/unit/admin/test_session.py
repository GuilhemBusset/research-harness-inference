"""The admin's session switch (`rhi_admin.session`), through the CLI.

The pool-state table, STS and SSM, which holds the example configuration, are moto's.
CloudFormation is stubbed and answers for the deployed stack's resources by logical ID. `status`
prints the pools' sessions after the switch's line (`test_sessions.py`): Lambda is stubbed for it,
and its scaler has no pool.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from rhi import config
from rhi_admin import main
from rhi_admin import session as admin_session
from rhi_common.records import SessionSwitch
from rhi_common.tables import SESSION_SWITCH_KEY

NOW = 1_800_000_000
TABLE = "deployed-pool-state"
SCALER = "deployed-scaler"
# moto's caller: its account, the placeholder one, is not part of who switched the session.
WHO = "user/moto"

type Run = Callable[..., tuple[int, str]]


class _Session:
    """The caller's session, with CloudFormation and Lambda stubbed."""

    def __init__(self, real: Any, stubbed: dict[str, Any]) -> None:
        self.real, self.stubbed = real, stubbed

    def client(self, name: str) -> Any:
        return self.stubbed.get(name) or self.real.client(name)

    def resource(self, name: str) -> Any:
        return self.real.resource(name)


def _detail(logical_id: str, physical_id: str) -> dict[str, Any]:
    return {
        "StackResourceDetail": {
            "LogicalResourceId": logical_id,
            "PhysicalResourceId": physical_id,
            "ResourceType": "AWS::DynamoDB::Table",
            "LastUpdatedTimestamp": "2027-01-01T00:00:00Z",
            "ResourceStatus": "CREATE_COMPLETE",
        }
    }


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Iterator[Run]:
    """`session <command>` against the deployed pool-state table: its exit status and what it printed."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        real = boto3.session.Session()
        keys = [("pool", "HASH"), ("record", "RANGE")]
        real.resource("dynamodb").create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name, _ in keys],
            KeySchema=[{"AttributeName": name, "KeyType": kind} for name, kind in keys],
        )
        config.write_ssm(config.load({"RHI_CONFIG": "example"}), real.client("ssm"))
        cloudformation, functions = real.client("cloudformation"), real.client("lambda")
        with Stubber(cloudformation) as stub, Stubber(functions) as functions_stub:
            for logical_id in (main.ROSTER_LOGICAL_ID, main.KEYS_LOGICAL_ID):
                expected = {"StackName": main.STACK_NAME, "LogicalResourceId": logical_id}
                stub.add_response("describe_stack_resource", _detail(logical_id, f"deployed-{logical_id}"), expected)
            admin = main.connect(_Session(real, {"cloudformation": cloudformation, "lambda": functions}))

            def session(command: str, now: int = NOW) -> tuple[int, str]:
                # The switch's table; then, for `status`, what the pools' sessions are read from.
                found = [(main.POOL_STATE_LOGICAL_ID, TABLE)]
                if command == "status":
                    found += [(main.POOL_STATE_LOGICAL_ID, TABLE), (main.SCALER_FUNCTION_LOGICAL_ID, SCALER)]
                    settings = {"FunctionName": SCALER, "Environment": {"Variables": {"RHI_SCALER_SETTINGS": '{"pools": {}}'}}}
                    functions_stub.add_response("get_function_configuration", settings, {"FunctionName": SCALER})
                for logical_id, physical_id in found:
                    expected = {"StackName": main.POOLS_STACK_NAME, "LogicalResourceId": logical_id}
                    stub.add_response("describe_stack_resource", _detail(logical_id, physical_id), expected)
                out = io.StringIO()
                status = main.run(["session", command], admin, now=now, out=out)
                stub.assert_no_pending_responses()
                functions_stub.assert_no_pending_responses()
                return status, out.getvalue()

            yield session


def rows(out: str) -> list[list[str]]:
    """The switch's line of `status`: the one after its header, before the pools' lines."""
    return [line.split("\t") for line in out.splitlines()[1:2]]


def test_the_switch_reads_off_until_switched_on_then_reads_back_who_and_when(run: Run) -> None:
    status, out = run("status")
    assert (status, rows(out)) == (0, [["off", "-", "-"]])

    assert run("on")[0] == 0
    assert rows(run("status")[1]) == [["on", WHO, str(NOW)]]

    # Switching it on again writes nothing: the time it carries is still when it was switched on.
    assert run("on", now=NOW + 60)[0] == 0
    assert rows(run("status")[1]) == [["on", WHO, str(NOW)]]

    assert run("off", now=NOW + 3600)[0] == 0
    assert rows(run("status")[1]) == [["off", WHO, str(NOW + 3600)]]
    items = boto3.resource("dynamodb").Table(TABLE).scan()["Items"]
    assert [{key: item[key] for key in SESSION_SWITCH_KEY} for item in items] == [SESSION_SWITCH_KEY]


def test_an_off_command_that_races_another_keeps_the_first_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two admins switch the session off at once: the one that writes first is who ended it, and when."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        keys = [("pool", "HASH"), ("record", "RANGE")]
        table = boto3.resource("dynamodb").create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name, _ in keys],
            KeySchema=[{"AttributeName": name, "KeyType": kind} for name, kind in keys],
        )
        first = admin_session.Switch(table=table, caller=lambda: "user/first")
        admin_session.turn(True, switch=first, now=NOW)
        read = table.get_item

        def read_then_first_switches_off(**kwargs: Any) -> Any:
            item = read(**kwargs)  # the second command has read the switch on
            monkeypatch.setattr(table, "get_item", read)
            assert admin_session.turn(False, switch=first, now=NOW + 20)[1]
            return item

        monkeypatch.setattr(table, "get_item", read_then_first_switches_off)
        second = admin_session.Switch(table=table, caller=lambda: "user/second")
        state, written = admin_session.turn(False, switch=second, now=NOW + 21)

        assert not written
        assert state == SessionSwitch(on=False, set_by="user/first", set_at=NOW + 20)
        assert admin_session.status(first) == state
