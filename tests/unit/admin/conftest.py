"""What the admin CLI's tests run against: tables, secrets and a parameter that moto holds.

The tables are found as a real run finds them, through the access stack's resources by logical
ID, which a stub of CloudFormation answers. pytest finds the fixture `admin` by its name, so a
test file imports nothing from here.
"""

from __future__ import annotations

import datetime
import secrets
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from rhi_admin import main
from rhi_common.tables import KEYS, ROSTER, KeyAttribute, Table

if TYPE_CHECKING:
    from collections.abc import Iterator

COURSE_END = datetime.date(2027, 6, 30)


def schema(*keys: KeyAttribute | None) -> list[dict[str, str]]:
    present = [key for key in keys if key is not None]
    return [{"AttributeName": k.name, "KeyType": kind} for k, kind in zip(present, ("HASH", "RANGE"), strict=False)]


def _create(dynamodb: Any, table: Table) -> Any:
    keyed = {table.partition_key, table.sort_key} | {
        key for index in table.indexes for key in (index.partition_key, index.sort_key)
    }
    return dynamodb.create_table(
        TableName=f"{table.name}-{secrets.token_hex(4)}",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": k.name, "AttributeType": k.type.value} for k in keyed if k is not None],
        KeySchema=schema(table.partition_key, table.sort_key),
        GlobalSecondaryIndexes=[
            {
                "IndexName": i.name,
                "KeySchema": schema(i.partition_key, i.sort_key),
                "Projection": {"ProjectionType": "ALL"},
            }
            for i in table.indexes
        ],
    )


class _Session:
    """The caller's session, with CloudFormation stubbed: moto's tables stand for the deployed stack's."""

    def __init__(self, real: Any, cloudformation: Any) -> None:
        self.real, self.cloudformation = real, cloudformation

    def client(self, name: str) -> Any:
        return self.cloudformation if name == "cloudformation" else self.real.client(name)

    def resource(self, name: str) -> Any:
        return self.real.resource(name)


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> Iterator[main.Admin]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        real = boto3.session.Session()
        dynamodb = real.resource("dynamodb")
        names = {
            main.ROSTER_LOGICAL_ID: _create(dynamodb, ROSTER).name,
            main.KEYS_LOGICAL_ID: _create(dynamodb, KEYS).name,
        }
        real.client("ssm").put_parameter(Name=main.COURSE_END_PARAMETER, Value=COURSE_END.isoformat(), Type="String")
        cloudformation = real.client("cloudformation")
        with Stubber(cloudformation) as stub:
            for logical_id, name in names.items():
                detail = {
                    "LogicalResourceId": logical_id,
                    "PhysicalResourceId": name,
                    "ResourceType": "AWS::DynamoDB::Table",
                    "LastUpdatedTimestamp": datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
                    "ResourceStatus": "CREATE_COMPLETE",
                }
                expected = {"StackName": main.STACK_NAME, "LogicalResourceId": logical_id}
                stub.add_response("describe_stack_resource", {"StackResourceDetail": detail}, expected)
            connected = main.connect(_Session(real, cloudformation))
            stub.assert_no_pending_responses()
        yield connected
