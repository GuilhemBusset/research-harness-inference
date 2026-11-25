"""The admin's commands on the pools' operating state (`rhi_admin.pools`), through the CLI.

The pool-state table, STS and SSM are moto's. CloudFormation and Lambda are stubbed: the stub
answers for the deployed stack's resources by logical ID, and for the scaler's function, its
settings and its runs.
"""

from __future__ import annotations

import datetime
import io
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from moto import mock_aws

from rhi_admin import main
from rhi_admin.pools import identity_name
from rhi_common.records import CONFIGURED_MODE, Mode
from rhi_scaler.aws_io import memory_from_item, memory_update
from rhi_scaler.settings import SETTINGS_VARIABLE, parse_settings

NOW = 1_800_000_000
SCALER = "deployed-scaler"
TABLE = "deployed-pool-state"
# moto's caller: its account, the placeholder one, is not part of who made a change.
WHO = "user/moto"


def entry(wake: str) -> dict[str, Any]:
    return {
        "engine": "mock",
        "instance_types": ["g6e.xlarge"],
        "prices": {"g6e.xlarge": 1.86},
        "max_instances": 4,
        "warm_instances": 1,
        "wake": wake,
        "target_concurrency": 8,
        "context_length": 32_768,
        "largest_output": 4_096,
    }


# The scaler's settings as its construct gives them: one pool of each `wake`.
SETTINGS = json.dumps(
    {
        "pools": {
            name: {"entry": entry(wake), "group": f"{name}-group", "target_group": f"{name}-targets"}
            for name, wake in (("coursework", "scheduled-only"), ("mock", "on-demand"))
        },
        "heartbeat_table": "deployed-heartbeats",
        "pool_state_table": TABLE,
        "workspace_url": "https://example.com/api/v1/remote_write",
    }
)


class _Session:
    """The caller's session, with CloudFormation and Lambda stubbed."""

    def __init__(self, real: Any, stubbed: dict[str, Any]) -> None:
        self.real, self.stubbed = real, stubbed

    def client(self, name: str) -> Any:
        return self.stubbed.get(name) or self.real.client(name)

    def resource(self, name: str) -> Any:
        return self.real.resource(name)


def _detail(logical_id: str, physical_id: str, kind: str) -> dict[str, Any]:
    return {
        "StackResourceDetail": {
            "LogicalResourceId": logical_id,
            "PhysicalResourceId": physical_id,
            "ResourceType": kind,
            "LastUpdatedTimestamp": datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
            "ResourceStatus": "CREATE_COMPLETE",
        }
    }


@dataclass
class Deployed:
    """The CLI against the deployed stack: `run` queues what a pool command reads of it."""

    admin: main.Admin
    table: Any
    cloudformation: Stubber
    functions: Stubber

    def run(
        self, *argv: str, now: int = NOW, settings: str | None = SETTINGS, parked: bool = False
    ) -> tuple[int, str, str]:
        for logical_id, physical_id, kind in (
            (main.POOL_STATE_LOGICAL_ID, TABLE, "AWS::DynamoDB::Table"),
            (main.SCALER_FUNCTION_LOGICAL_ID, SCALER, "AWS::Lambda::Function"),
        ):
            expected = {"StackName": main.POOLS_STACK_NAME, "LogicalResourceId": logical_id}
            if parked and physical_id == SCALER:
                # A parked stage's stack holds no scaler: CloudFormation knows no such resource in it.
                self.cloudformation.add_client_error("describe_stack_resource", "ValidationError", expected_params=expected)
                continue
            self.cloudformation.add_response("describe_stack_resource", _detail(logical_id, physical_id, kind), expected)
        if settings is not None and not parked:
            answer = {"FunctionName": SCALER, "Environment": {"Variables": {SETTINGS_VARIABLE: settings}}}
            self.functions.add_response("get_function_configuration", answer, {"FunctionName": SCALER})
        out, err = io.StringIO(), io.StringIO()
        status = main.run(["pool", *argv], self.admin, now=now, out=out, err=err)
        self.cloudformation.assert_no_pending_responses()
        self.functions.assert_no_pending_responses()
        return status, out.getvalue(), err.getvalue()

    def items(self) -> list[dict[str, Any]]:
        return self.table.scan()["Items"]

    def item(self, pool: str, record: str) -> dict[str, Any] | None:
        return self.table.get_item(Key={"pool": pool, "record": record}, ConsistentRead=True).get("Item")


@pytest.fixture
def deployed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Deployed]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        real = boto3.session.Session()
        keys = [("pool", "HASH"), ("record", "RANGE")]
        table = real.resource("dynamodb").create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name, _ in keys],
            KeySchema=[{"AttributeName": name, "KeyType": kind} for name, kind in keys],
        )
        cloudformation, functions = real.client("cloudformation"), real.client("lambda")
        with Stubber(cloudformation) as cloudformation_stub, Stubber(functions) as functions_stub:
            # The access stack's two tables, which `connect` finds before any command runs.
            for logical_id in (main.ROSTER_LOGICAL_ID, main.KEYS_LOGICAL_ID):
                expected = {"StackName": main.STACK_NAME, "LogicalResourceId": logical_id}
                answer = _detail(logical_id, f"deployed-{logical_id}", "AWS::DynamoDB::Table")
                cloudformation_stub.add_response("describe_stack_resource", answer, expected)
            admin = main.connect(_Session(real, {"cloudformation": cloudformation, "lambda": functions}))
            yield Deployed(admin, table, cloudformation_stub, functions_stub)


def test_disabling_and_re_enabling_write_the_mode_with_who_and_when(deployed: Deployed) -> None:
    assert deployed.run("disable", "mock")[0] == 0
    assert deployed.item("mock", "mode") == {"pool": "mock", "record": "mode", "mode": "disabled", "set_by": WHO, "set_at": NOW}

    status, out, _ = deployed.run("enable", "mock", now=NOW + 60)
    assert status == 0 and "enabled" in out
    assert deployed.item("mock", "mode") == {"pool": "mock", "record": "mode", "mode": "enabled", "set_by": WHO, "set_at": NOW + 60}

    # Re-enabled to its configured mode: a scheduled-only pool is not woken on demand.
    deployed.run("disable", "coursework")
    deployed.run("enable", "coursework", now=NOW + 120)
    assert deployed.item("coursework", "mode") == {
        "pool": "coursework", "record": "mode", "mode": "scheduled-only", "set_by": WHO, "set_at": NOW + 120,
    }


def test_a_pool_priced_above_the_default_limit_can_be_disabled(deployed: Deployed) -> None:
    # The deployment checked its prices against the configuration's limit, which may be above the default.
    settings = json.loads(SETTINGS)
    settings["pools"]["mock"]["entry"]["prices"] = {"g6e.xlarge": 6.0}
    status, _, err = deployed.run("disable", "mock", settings=json.dumps(settings))
    assert (status, err) == (0, "")
    assert deployed.item("mock", "mode") == {"pool": "mock", "record": "mode", "mode": "disabled", "set_by": WHO, "set_at": NOW}


def test_status_shows_the_deployed_configured_mode_until_the_admin_sets_one(deployed: Deployed) -> None:
    # The configuration in SSM may say otherwise before the next deployment: the running pools are what counts.
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name="/rhi/config/pools/coursework", Value=json.dumps(entry("on-demand")), Type="String")
    status, out, _ = deployed.run("status")
    assert status == 0
    rows = [line.split("\t") for line in out.splitlines()[1:]]
    assert rows == [["coursework", "scheduled-only", "configured", "-", "false"], ["mock", "enabled", "configured", "-", "false"]]
    assert deployed.items() == []

    deployed.run("disable", "mock")
    deployed.table.put_item(Item={"pool": "coursework", "record": "memory", "launch_suspended": True, "waiting_since": {}})
    rows = [line.split("\t") for line in deployed.run("status")[1].splitlines()[1:]]
    assert rows == [["coursework", "scheduled-only", "configured", "-", "true"], ["mock", "disabled", WHO, str(NOW), "false"]]


def test_clearing_the_suspension_changes_the_mark_alone_and_survives_the_scaler(deployed: Deployed) -> None:
    memory = {"pool": "mock", "record": "memory", "launch_suspended": True, "suspended_at": NOW - 600, "waiting_since": {}}
    deployed.table.put_item(Item={**memory, "last_request_at": NOW - 900})
    status, out, _ = deployed.run("clear-suspension", "mock")
    assert status == 0 and WHO in out
    cleared = {**memory, "last_request_at": NOW - 900, "launch_suspended": False, "cleared_by": WHO, "cleared_at": NOW}
    assert deployed.item("mock", "memory") == cleared

    # A scaler run that read the mark set before the admin cleared it writes after: the mark stays
    # cleared, and so do who cleared it and when.
    suspended = memory_from_item(memory)
    later = memory_from_item({**memory, "last_request_at": NOW + 30})
    boto3.client("dynamodb").update_item(**memory_update(TABLE, "mock", suspended, later))
    stored = deployed.item("mock", "memory")
    assert stored is not None
    assert (stored["launch_suspended"], stored["cleared_by"], stored["cleared_at"]) == (False, WHO, NOW)

    # Nothing left to clear, and no record at all: refused, nothing written.
    for pool in ("mock", "coursework"):
        status, _, err = deployed.run("clear-suspension", pool)
        assert status == 1 and "not suspended" in err
    assert deployed.item("coursework", "memory") is None


def test_asking_for_a_run_invokes_the_function_found_by_its_logical_id(deployed: Deployed) -> None:
    decisions = {"pools": [{"pool": "coursework", "desired": 0, "reason": "unchanged"}, {"pool": "mock", "desired": 1, "reason": "wake"}]}
    body = json.dumps(decisions).encode()
    expected = {"FunctionName": SCALER, "InvocationType": "RequestResponse", "Payload": b"{}"}
    deployed.functions.add_response("invoke", {"StatusCode": 200, "Payload": StreamingBody(io.BytesIO(body), len(body))}, expected)
    status, out, _ = deployed.run("run", settings=None)
    assert status == 0
    assert out.splitlines() == ["coursework\t0\tunchanged", "mock\t1\twake"]

    # A run under way: the function's one reserved invocation is taken.
    under_way = {"Reason": "ReservedFunctionConcurrentInvocationLimitExceeded"}
    deployed.functions.add_client_error("invoke", "TooManyRequestsException", http_status_code=429, modeled_fields=under_way)
    status, _, err = deployed.run("run", settings=None)
    assert status == 1 and "already running" in err


def test_a_pool_that_is_not_deployed_is_refused(deployed: Deployed) -> None:
    for command in ("disable", "enable", "clear-suspension"):
        status, _, err = deployed.run(command, "other")
        assert status == 1 and "no deployed pool" in err
    assert deployed.items() == []


def test_a_parked_stage_has_no_pool(deployed: Deployed) -> None:
    status, out, err = deployed.run("status", parked=True)
    assert (status, out.splitlines()[1:], err) == (0, [], "")
    # What needs a pool or the scaler is refused, and says that none is deployed.
    for argv in (["run"], ["disable", "mock"], ["enable", "mock"], ["clear-suspension", "mock"]):
        status, _, err = deployed.run(*argv, parked=True)
        assert status == 1 and "no pool is deployed" in err
    assert deployed.items() == []


@pytest.mark.parametrize(
    "argv",
    [["parked", "mock"], ["park", "mock"], ["disable", "mock", "--mode", "parked"], ["enable", "mock", "parked"]],
)
def test_no_command_parks_a_pool(deployed: Deployed, argv: Sequence[str]) -> None:
    assert "parked" not in {mode.value for mode in Mode}
    with pytest.raises(SystemExit):
        main.run(["pool", *argv], deployed.admin, now=NOW, out=io.StringIO(), err=io.StringIO())
    # What every command writes is one of the admin's modes, never `parked`.
    for argv_ok in (["disable", "mock"], ["enable", "mock"], ["disable", "coursework"], ["enable", "coursework"]):
        deployed.run(*argv_ok)
    assert {item["mode"] for item in deployed.items()} <= {Mode.ENABLED.value, Mode.SCHEDULED_ONLY.value, Mode.DISABLED.value}


def test_the_settings_read_are_the_scalers_own() -> None:
    # The scaler's settings give a pool no mode: the configured one follows the pool's `wake`.
    pools = [pool.config for pool in parse_settings(SETTINGS, "us-east-1").pools]
    modes = {pool.name: CONFIGURED_MODE[pool.wake] for pool in pools}
    assert modes == {"coursework": Mode.SCHEDULED_ONLY, "mock": Mode.ENABLED}


def test_who_is_the_identity_without_its_account() -> None:
    assert identity_name("arn:aws:sts::123456789012:assumed-role/Admin/session") == "assumed-role/Admin/session"
    assert identity_name("arn:aws:iam::123456789012:user/admin") == "user/admin"
