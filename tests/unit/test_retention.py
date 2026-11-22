"""Every row of personal data expires by the course end plus 90 days (system design, section 6.6).

The usage rows and the roster rows expire then; a key row, whose validity ends at the course end
(section 6.4), expires no later. Each writer is run against tables that moto holds. The admin CLI
is run as a real run is, with the course end read from a configuration that moto's SSM holds. A
key a test needs is generated when it runs.
"""

from __future__ import annotations

import dataclasses
import datetime
import io
import secrets
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

from rhi import config
from rhi_admin import keys as admin_keys
from rhi_admin import main, privacy
from rhi_admin import roster as admin_roster
from rhi_common import retention
from rhi_common.keys import generate_key, key_hash
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import KEYS, ROSTER, USAGE, Table
from rhi_gateway.budget import DynamoDBLedger, InMemoryLedger, Ledger, Reservation
from rhi_issuer import mint, revoke, rules

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

COURSE_END = datetime.date(2027, 1, 15)
# 15 April 2027, the course end plus 90 days, as DynamoDB's expiry reads it.
EXPIRY = int(datetime.datetime(2027, 4, 15, tzinfo=datetime.UTC).timestamp())
NOW = int(datetime.datetime(2026, 11, 15, 12, tzinfo=datetime.UTC).timestamp())
DAY = "2026-11-15"
GITHUB_ID = 7


def test_a_personal_row_of_a_course_that_ends_on_15_january_2027_expires_on_15_april_2027() -> None:
    assert retention.expires_at(COURSE_END) == EXPIRY


def _create(dynamodb: Any, table: Table) -> Any:
    keyed = {table.partition_key, table.sort_key} | {
        key for index in table.indexes for key in (index.partition_key, index.sort_key)
    }

    def schema(*keys: Any) -> list[dict[str, str]]:
        present = [key for key in keys if key is not None]
        return [{"AttributeName": key.name, "KeyType": kind} for key, kind in zip(present, ("HASH", "RANGE"))]

    indexes = [
        {"IndexName": i.name, "KeySchema": schema(i.partition_key, i.sort_key), "Projection": {"ProjectionType": "ALL"}}
        for i in table.indexes
    ]
    return dynamodb.create_table(
        TableName=f"{table.name}-{secrets.token_hex(4)}",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": k.name, "AttributeType": k.type.value} for k in keyed if k is not None],
        KeySchema=schema(table.partition_key, table.sort_key),
        **({"GlobalSecondaryIndexes": indexes} if indexes else {}),
    )


class _Stacks:
    """CloudFormation as the admin CLI reads it: each table's name, by its logical ID."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = names

    def describe_stack_resource(self, *, StackName: str, LogicalResourceId: str) -> dict[str, Any]:
        return {"StackResourceDetail": {"PhysicalResourceId": self.names[LogicalResourceId]}}


class _Session:
    """The admin's session: moto for everything, but CloudFormation."""

    def __init__(self, stacks: _Stacks) -> None:
        self.real, self.stacks = boto3.session.Session(), stacks

    def client(self, name: str) -> Any:
        return self.stacks if name == "cloudformation" else self.real.client(name)

    def resource(self, name: str) -> Any:
        return self.real.resource(name)


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        usage, roster, keys = (_create(dynamodb, table) for table in (USAGE, ROSTER, KEYS))
        # The owner's configuration, whose course end is 15 January 2027.
        example = config.load({"RHI_CONFIG": "example"})
        config.write_ssm(dataclasses.replace(example, course_end=COURSE_END), boto3.client("ssm"))
        stacks = _Stacks({main.ROSTER_LOGICAL_ID: roster.name, main.KEYS_LOGICAL_ID: keys.name})
        yield SimpleNamespace(usage=usage, roster=roster, keys=keys, admin=main.connect(_Session(stacks)))


def _student() -> StudentKey:
    return StudentKey(
        key_hash=key_hash(generate_key()),
        github_id=GITHUB_ID,
        alias="otter",
        installation=None,
        issued_at=NOW - 86_400,
        expires_at=NOW + 30 * 86_400,
        revoked_at=None,
        daily_token_budget=1_000_000,
        concurrency_limit=4,
    )


def _reserve(ledger: Ledger, request: str) -> Reservation:
    return ledger.reserve(
        _student(), request, b"x" * 400, output_limit=100, hold_seconds=420, longest_request_seconds=1800, pool="mock"
    )


def test_every_usage_row_the_in_memory_ledger_writes_expires_at_the_course_end_plus_90_days() -> None:
    ledger = InMemoryLedger(course_end=COURSE_END, clock=lambda: float(NOW))
    expiries = []
    first = _reserve(ledger, "request-1")
    expiries.append(ledger.day(GITHUB_ID, DAY))
    second = _reserve(ledger, "request-2")
    expiries.append(ledger.day(GITHUB_ID, DAY))
    ledger.settle(first, input_tokens=100, output_tokens=10, ended_cleanly=True, cookie="a-cookie")
    expiries.append(ledger.day(GITHUB_ID, DAY))
    ledger.settle(second, input_tokens=100, output_tokens=10, ended_cleanly=True)
    expiries.append(ledger.day(GITHUB_ID, DAY))

    assert [day.expires_at if day else None for day in expiries] == [EXPIRY] * 4


def test_every_usage_row_the_dynamodb_ledger_writes_expires_at_the_course_end_plus_90_days(
    aws: SimpleNamespace,
) -> None:
    ledger = DynamoDBLedger(boto3.client("dynamodb"), aws.usage.name, course_end=COURSE_END, clock=lambda: float(NOW))
    key = {"github_id": GITHUB_ID, "day": DAY}

    def stored() -> int:
        return int(aws.usage.get_item(Key=key, ConsistentRead=True)["Item"]["expires_at"])

    def written_before(expires_at: int) -> None:
        """The row as an earlier rule left it: the key's course end plus 91 days."""
        aws.usage.update_item(
            Key=key, UpdateExpression="SET expires_at = :later", ExpressionAttributeValues={":later": expires_at}
        )

    # The day's first reservation writes the row whole.
    first = _reserve(ledger, "request-1")
    assert stored() == EXPIRY
    # Every later write of the row sets it again: a reservation, a settlement and a withdrawal.
    written_before(EXPIRY + 86_400)
    second = _reserve(ledger, "request-2")
    assert stored() == EXPIRY
    written_before(EXPIRY + 86_400)
    ledger.settle(first, input_tokens=100, output_tokens=10, ended_cleanly=True, cookie="a-cookie")
    assert stored() == EXPIRY
    written_before(EXPIRY + 86_400)
    ledger.withdraw(second)
    assert stored() == EXPIRY


def test_every_roster_row_the_admin_cli_writes_expires_at_the_course_end_plus_90_days(aws: SimpleNamespace) -> None:
    # The CLI reads the course end from the configuration in SSM.
    assert aws.admin.course_end() == COURSE_END

    assert main.run(["roster", "add", str(GITHUB_ID), "otter"], aws.admin, now=NOW, out=io.StringIO()) == 0
    entry = {ROSTER.partition_key.name: GITHUB_ID}
    claim = {ROSTER.partition_key.name: admin_roster.held_alias_id("otter")}
    added = [aws.roster.get_item(Key=item, ConsistentRead=True)["Item"]["expires_at"] for item in (entry, claim)]
    assert added == [EXPIRY, EXPIRY]

    # A change writes the entry, and its expiry with it.
    aws.roster.update_item(
        Key=entry, UpdateExpression="SET expires_at = :later", ExpressionAttributeValues={":later": EXPIRY + 86_400}
    )
    assert main.run(["roster", "set", str(GITHUB_ID), "--budget", "5"], aws.admin, now=NOW, out=io.StringIO()) == 0
    changed = aws.roster.get_item(Key=entry, ConsistentRead=True)["Item"]
    assert (changed["daily_token_budget"], changed["expires_at"]) == (5, EXPIRY)


def test_every_key_row_the_admin_cli_writes_expires_no_later_than_the_course_end_plus_90_days(
    aws: SimpleNamespace,
) -> None:
    assert main.run(["roster", "add", str(GITHUB_ID), "otter"], aws.admin, now=NOW, out=io.StringIO()) == 0
    printed = io.StringIO()

    assert main.run(["key", "issue", str(GITHUB_ID)], aws.admin, now=NOW, out=printed) == 0

    item = aws.keys.get_item(Key={KEYS.partition_key.name: key_hash(printed.getvalue().strip())})["Item"]
    assert NOW < item["expires_at"] <= EXPIRY


def _entry() -> RosterEntry:
    return RosterEntry(
        github_id=GITHUB_ID,
        alias="otter",
        daily_token_budget=1_000_000,
        concurrency_limit=4,
        added_at=NOW - 86_400,
        expires_at=EXPIRY,
    )


def test_every_key_row_the_issuer_writes_expires_no_later_than_the_course_end_plus_90_days(
    aws: SimpleNamespace,
) -> None:
    first = rules.issue(_entry(), "installation-1", course_end=COURSE_END, now=NOW, keys=aws.keys)
    second = rules.issue(_entry(), "installation-1", course_end=COURSE_END, now=NOW + 60, keys=aws.keys)

    # The first key is revoked by the second, in the write that stores it: both rows are there.
    items = [aws.keys.get_item(Key={KEYS.partition_key.name: key_hash(m.key)})["Item"] for m in (first, second)]
    assert ["revoked_at" in item for item in items] == [True, False]
    assert all(NOW < item["expires_at"] <= EXPIRY for item in items)


def test_the_lock_a_stopped_login_leaves_in_the_keys_table_expires_no_later_than_the_course_end_plus_90_days(
    aws: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stops(*_arguments: object, **_keywords: object) -> None:
        raise RuntimeError("the function stopped")

    # The function stops after it took the student's lock and before the write that deletes it.
    monkeypatch.setattr(rules, "mint", stops)
    with pytest.raises(RuntimeError):
        rules.issue(_entry(), "installation-1", course_end=COURSE_END, now=NOW, keys=aws.keys)

    lock = aws.keys.get_item(Key=rules.lock_key(GITHUB_ID), ConsistentRead=True)["Item"]
    assert NOW < lock["expires_at"] <= EXPIRY


# The course end of a configuration before the owner moved it earlier, to the 15 January 2027 in
# SSM: a key issued then works until 1 July 2027, past the 15 April 2027 every row now expires by.
LATER_END = datetime.date(2027, 6, 30)
# A key of a still earlier configuration, which expires on 1 January 2027: a later write keeps it.
EARLIER_END = datetime.date(2026, 12, 31)


def _stores(aws: SimpleNamespace, during_sleep: Callable[[], None]) -> privacy.Stores:
    """What an erase reaches beyond the roster and the keys; its waits run `during_sleep`."""
    return privacy.Stores(
        usage=aws.usage, logs=None, log_group="", query=lambda *_: [], sleep=lambda _seconds: during_sleep()
    )


def _expiries(table: Any) -> list[int]:
    return [int(item["expires_at"]) for item in table.scan(ConsistentRead=True)["Items"]]


@pytest.mark.parametrize("command", ["remove", "erase"])
def test_the_rows_that_take_a_student_off_the_roster_expire_by_the_course_end_plus_90_days(
    aws: SimpleNamespace, command: str
) -> None:
    seen: list[int] = []
    admin = dataclasses.replace(aws.admin, privacy=lambda: _stores(aws, lambda: seen.extend(_expiries(aws.roster))))
    assert main.run(["roster", "add", str(GITHUB_ID), "otter"], admin, now=NOW, out=io.StringIO()) == 0

    # On the course's last day for a removal, and an hour before the rows expire for an erase: the
    # alias's hold and the erase's own hold of the GitHub ID would otherwise outlive them.
    at = int(datetime.datetime(2027, 1, 15, tzinfo=datetime.UTC).timestamp()) if command == "remove" else EXPIRY - 3600
    assert main.run(["privacy" if command == "erase" else "roster", command, str(GITHUB_ID)], admin, now=at) == 0

    seen.extend(_expiries(aws.roster))
    assert seen and max(seen) <= EXPIRY


@pytest.mark.parametrize("command", [["roster", "set", "--limit", "2"], ["key", "revoke"], ["roster", "remove"], ["privacy", "erase"]])
def test_every_key_row_the_admin_cli_writes_again_expires_by_the_course_end_plus_90_days(
    aws: SimpleNamespace, command: list[str]
) -> None:
    seen: list[dict[str, int]] = []
    admin = dataclasses.replace(aws.admin, privacy=lambda: _stores(aws, lambda: seen.append(by_hash())))
    assert main.run(["roster", "add", str(GITHUB_ID), "otter"], admin, now=NOW, out=io.StringIO()) == 0
    later, earlier = (
        key_hash(admin_keys.issue(GITHUB_ID, roster=aws.roster, keys=aws.keys, course_end=end, now=NOW))
        for end in (LATER_END, EARLIER_END)
    )

    def by_hash() -> dict[str, int]:
        return {item["key_hash"]: int(item["expires_at"]) for item in aws.keys.scan(ConsistentRead=True)["Items"]}

    kept = by_hash()[earlier]
    assert by_hash()[later] > EXPIRY
    argv = [*command[:2], str(GITHUB_ID), *command[2:]]
    assert main.run(argv, admin, now=NOW + 60, out=io.StringIO()) == 0

    # A key `roster set` writes stays in use, so it stops working at the new course end; a revoked
    # key may stay until the rows expire. An erase deletes the keys once its waits are over: what
    # they hold meanwhile is what counts.
    rewritten = mint.expiry(COURSE_END) if command[0:2] == ["roster", "set"] else EXPIRY
    for keys in seen or [by_hash()]:
        assert keys == {later: rewritten, earlier: kept}


def test_revoking_a_key_row_without_an_expiry_gives_it_the_course_end_plus_90_days(aws: SimpleNamespace) -> None:
    key = rules.issue(_entry(), "installation-1", course_end=COURSE_END, now=NOW, keys=aws.keys).key
    row = {KEYS.partition_key.name: key_hash(key)}
    aws.keys.update_item(Key=row, UpdateExpression="REMOVE expires_at")

    revoke.revoke_key(key, keys=aws.keys, now=NOW + 60, course_end=COURSE_END)

    item = aws.keys.get_item(Key=row, ConsistentRead=True)["Item"]
    assert ("revoked_at" in item, item.get("expires_at")) == (True, EXPIRY)


@pytest.mark.parametrize("write", ["replace", "login elsewhere", "revoke", "revoke all"])
def test_every_key_row_the_issuer_writes_again_expires_by_the_course_end_plus_90_days(
    aws: SimpleNamespace, write: str
) -> None:
    other = rules.issue(_entry(), "installation-2", course_end=EARLIER_END, now=NOW, keys=aws.keys)
    first = rules.issue(_entry(), "installation-1", course_end=LATER_END, now=NOW + 1, keys=aws.keys)
    kept = int(aws.keys.get_item(Key={KEYS.partition_key.name: key_hash(other.key)})["Item"]["expires_at"])

    if write == "replace":
        # A login on the first installation, once the course end is 15 January 2027.
        rules.issue(_entry(), "installation-1", course_end=COURSE_END, now=NOW + 60, keys=aws.keys)
    elif write == "login elsewhere":
        # A login on the other installation: the first key stays in use, and names its successor.
        rules.issue(_entry(), "installation-2", course_end=COURSE_END, now=NOW + 60, keys=aws.keys)
    elif write == "revoke":
        revoke.revoke_key(first.key, keys=aws.keys, now=NOW + 60, course_end=COURSE_END)
        revoke.revoke_key(other.key, keys=aws.keys, now=NOW + 60, course_end=COURSE_END)
    else:
        assert revoke.revoke_all(GITHUB_ID, keys=aws.keys, now=NOW + 60, course_end=COURSE_END) == 2

    def expiry(minted: Any) -> tuple[int, bool]:
        item = aws.keys.get_item(Key={KEYS.partition_key.name: key_hash(minted.key)}, ConsistentRead=True)["Item"]
        return int(item["expires_at"]), "revoked_at" in item

    # A key left in use stops working at the course end; a revoked one may stay until the rows expire.
    assert expiry(first) == ((mint.expiry(COURSE_END), False) if write == "login elsewhere" else (EXPIRY, True))
    assert expiry(other) == (kept, write != "replace")
