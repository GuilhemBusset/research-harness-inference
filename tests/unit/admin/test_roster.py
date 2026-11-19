"""The admin CLI's commands, against tables, a secret and a parameter that moto holds.

The tables are found as a real run finds them, through the access stack's resources by logical
ID, which a stub of CloudFormation answers. Every key comes from a command while the
tests run: none is written in this file.
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from rhi_admin import main
from rhi_admin.canary import CANARY_ALIAS, CANARY_GITHUB_ID, CANARY_SECRET
from rhi_common.keys import is_valid_key, key_hash
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import KEYS, ROSTER, KeyAttribute, Table

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO = Path(__file__).resolve().parents[3]
NOW = 1_800_000_000
COURSE_END = datetime.date(2027, 6, 30)
OTTER, HERON = 4242, 5151


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


def cli(admin: main.Admin, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    """Runs one command; its exit status, and everything it printed, out and err."""
    code = main.run(argv, admin, now=NOW)
    printed = capsys.readouterr()
    return code, printed.out + printed.err


def items(table: Any) -> list[dict[str, Any]]:
    return table.scan(ConsistentRead=True)["Items"]


def student_keys(admin: main.Admin, github_id: int) -> list[StudentKey]:
    return [StudentKey.from_item(item) for item in items(admin.keys) if item["github_id"] == github_id]


def test_an_entry_is_added_listed_and_removed(admin: main.Admin, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(admin, capsys, "roster", "add", str(OTTER), "otter")[0] == 0
    assert cli(admin, capsys, "roster", "add", str(HERON), "heron", "--budget", "0", "--limit", "2")[0] == 0
    # Without numbers, the defaults; a budget of 0 is kept.
    entries = {item["github_id"]: RosterEntry.from_item(item) for item in items(admin.roster)}
    assert (entries[OTTER].daily_token_budget, entries[OTTER].concurrency_limit) == (100_000_000, 4)
    assert (entries[HERON].daily_token_budget, entries[HERON].concurrency_limit) == (0, 2)
    code, listed = cli(admin, capsys, "roster", "list")
    assert code == 0
    assert re.search(rf"^{OTTER}\totter\t100000000\t4$", listed, re.MULTILINE)
    assert re.search(rf"^{HERON}\theron\t0\t2$", listed, re.MULTILINE)
    # A second entry for the ID, or the alias of another student, is refused.
    assert cli(admin, capsys, "roster", "add", str(OTTER), "stoat")[0] == 1
    assert cli(admin, capsys, "roster", "add", "6161", "otter")[0] == 1

    for _ in range(2):
        assert cli(admin, capsys, "key", "issue", str(OTTER))[0] == 0
    assert cli(admin, capsys, "roster", "remove", str(OTTER))[0] == 0

    assert str(OTTER) not in cli(admin, capsys, "roster", "list")[1]
    otter_keys = student_keys(admin, OTTER)
    assert len(otter_keys) == 2
    assert all(record.revoked_at == NOW for record in otter_keys)
    # Once off the roster, the student is issued no key.
    assert cli(admin, capsys, "key", "issue", str(OTTER))[0] == 1


def test_a_directly_issued_key_verifies_and_is_stored_as_its_sha256_only(
    admin: main.Admin, capsys: pytest.CaptureFixture[str]
) -> None:
    cli(admin, capsys, "roster", "add", str(OTTER), "otter", "--budget", "7000000", "--limit", "3")

    code, printed = cli(admin, capsys, "key", "issue", str(OTTER))

    assert code == 0
    key = printed.strip()
    assert is_valid_key(key)
    (item,) = items(admin.keys)
    record = StudentKey.from_item(item)
    assert record.key_hash == key_hash(key)
    assert (record.alias, record.daily_token_budget, record.concurrency_limit) == ("otter", 7_000_000, 3)
    assert record.installation is None
    assert record.revoked_at is None
    assert key not in json.dumps(items(admin.keys) + items(admin.roster), default=str)

    assert cli(admin, capsys, "key", "revoke", str(OTTER))[0] == 0
    assert student_keys(admin, OTTER)[0].revoked_at == NOW


def test_a_budget_change_reaches_the_keys(admin: main.Admin, capsys: pytest.CaptureFixture[str]) -> None:
    cli(admin, capsys, "roster", "add", str(OTTER), "otter")
    cli(admin, capsys, "roster", "add", str(HERON), "heron")
    issued = [cli(admin, capsys, "key", "issue", str(OTTER))[1].strip() for _ in range(3)]
    cli(admin, capsys, "key", "issue", str(HERON))
    revoked = issued[0]
    admin.keys.update_item(
        Key={"key_hash": key_hash(revoked)},
        UpdateExpression="SET revoked_at = :now",
        ExpressionAttributeValues={":now": NOW - 60},
    )

    assert cli(admin, capsys, "roster", "set", str(OTTER), "--budget", "250000", "--limit", "1")[0] == 0

    roster_item = admin.roster.get_item(Key={"github_id": OTTER})["Item"]
    assert (roster_item["daily_token_budget"], roster_item["concurrency_limit"]) == (250_000, 1)
    by_hash = {record.key_hash: record for record in student_keys(admin, OTTER)}
    for key in issued[1:]:
        assert (by_hash[key_hash(key)].daily_token_budget, by_hash[key_hash(key)].concurrency_limit) == (250_000, 1)
    assert (by_hash[key_hash(revoked)].daily_token_budget, by_hash[key_hash(revoked)].concurrency_limit) == (
        100_000_000,
        4,
    )
    # Another student's key is left alone.
    assert student_keys(admin, HERON)[0].daily_token_budget == 100_000_000


def test_the_canary_key_is_never_printed(admin: main.Admin, capsys: pytest.CaptureFixture[str]) -> None:
    outputs = []
    for _ in range(2):
        code, printed = cli(admin, capsys, "canary")
        assert code == 0
        outputs.append(printed)

    (secret,) = admin.secrets.list_secrets()["SecretList"]
    assert secret["Name"] == CANARY_SECRET
    key = admin.secrets.get_secret_value(SecretId=CANARY_SECRET)["SecretString"]
    assert is_valid_key(key)
    canary_keys = student_keys(admin, CANARY_GITHUB_ID)
    assert len(canary_keys) == 2
    (active,) = [record for record in canary_keys if record.revoked_at is None]
    assert active.key_hash == key_hash(key)
    assert active.alias == CANARY_ALIAS
    # The first key is neither active nor printed, and the second is not printed either.
    assert re.search(r"rhi_[0-9A-Za-z]{46}", "".join(outputs)) is None
    assert key not in "".join(outputs)


def test_the_tables_are_found_by_their_pinned_logical_ids() -> None:
    pinned = json.loads((REPO / "tests/policy/stateful_logical_ids.json").read_text())
    assert {main.KEYS_LOGICAL_ID, main.ROSTER_LOGICAL_ID} <= set(pinned[main.STACK_NAME])
    # The CLI names no account, no region and no table: the caller's credentials give them.
    source = "".join(path.read_text() for path in (REPO / "admin/rhi_admin").glob("*.py"))
    assert re.search(r"\b\d{12}\b", source) is None
    assert re.search(r"\b(?:us|eu|ap|sa|ca|me|af|il|mx)-[a-z]+-\d\b", source) is None
    assert "TableName" not in source
