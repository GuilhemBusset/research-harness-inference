"""Independent issuer rule checks using real moto tables and overlapping callers."""

from __future__ import annotations

import datetime
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.keys import key_hash
from rhi_common.records import RecordError
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import KEYS
from rhi_issuer import handler
from rhi_issuer.redeem import Refusal, Refused
from rhi_issuer.rules import issue

if TYPE_CHECKING:
    from collections.abc import Iterator

NOW = 1_800_000_000
END = datetime.date(2028, 1, 1)
STUDENT = RosterEntry(9182, "lynx", 1000000, 4, NOW - 100, NOW + 1000000)


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        index = KEYS.index("by_student")
        yield boto3.resource("dynamodb").create_table(
            TableName="issue88-probe-keys",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": KEYS.partition_key.name, "AttributeType": "S"},
                {"AttributeName": index.partition_key.name, "AttributeType": "N"},
                {"AttributeName": "issued_at", "AttributeType": "N"},
            ],
            KeySchema=[{"AttributeName": KEYS.partition_key.name, "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{
                "IndexName": index.name,
                "KeySchema": [
                    {"AttributeName": index.partition_key.name, "KeyType": "HASH"},
                    {"AttributeName": "issued_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )


def records(table: Any) -> list[StudentKey]:
    result = []
    for item in table.scan(ConsistentRead=True)["Items"]:
        try:
            result.append(StudentKey.from_item(item))
        except RecordError:
            continue
    return result


def issue_on(table: Any, installation: str, now: int = NOW, limit: int = 5) -> Any:
    return issue(STUDENT, installation, course_end=END, now=now, keys=table, issuances_per_day=limit)


def test_sequential_replacement_and_installation_refusal(table: Any) -> None:
    laptop, desktop, third = (secrets.token_hex(16) for _ in range(3))
    first = issue_on(table, laptop)
    second = issue_on(table, laptop, NOW + 1)
    issue_on(table, desktop, NOW + 2)
    state = {record.key_hash: record for record in records(table)}
    assert state[key_hash(first.key)].revoked_at is not None
    assert state[key_hash(second.key)].revoked_at is None
    assert len([record for record in state.values() if record.revoked_at is None]) == 2
    with pytest.raises(Refused) as refused:
        issue_on(table, third, NOW + 3)
    assert refused.value.refusal is Refusal.TOO_MANY_INSTALLATIONS
    assert "rhi revoke --all" in refused.value.refusal.sentence
    assert records(table) == list(state.values())


def test_daily_limit_counts_replacements_and_resets_at_utc_midnight(table: Any) -> None:
    installation = secrets.token_hex(16)
    midnight = NOW - NOW % 86400 + 86400
    for offset in range(5):
        issue_on(table, installation, midnight - 10 + offset)
    with pytest.raises(Refused) as refused:
        issue_on(table, installation, midnight - 1)
    assert refused.value.refusal is Refusal.TOO_MANY_ISSUANCES
    issue_on(table, installation, midnight)
    assert len(records(table)) == 6


def test_environment_limit_is_enforced_on_login(table: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RHI_CLIENT_ID", "Iv1." + secrets.token_hex(8))
    monkeypatch.setenv("RHI_COURSE_END", END.isoformat())
    monkeypatch.setenv("RHI_KEYS_TABLE", table.name)
    monkeypatch.setenv("RHI_ROSTER_TABLE", "probe-roster")
    monkeypatch.setenv("RHI_ISSUANCES_PER_DAY", "2")
    monkeypatch.setattr(handler, "_dynamodb", lambda: boto3.resource("dynamodb"))
    monkeypatch.setattr(handler, "redeem", lambda *args, **kwargs: STUDENT)
    issuer = handler.from_environment(clock=lambda: NOW)
    event = {
        "httpMethod": "POST", "path": "/auth/login",
        "body": json.dumps({"device_code": secrets.token_hex(20), "installation": secrets.token_hex(16)}),
    }
    assert issuer.answer(event)["statusCode"] == 200
    assert issuer.answer(event)["statusCode"] == 200
    answer = issuer.answer(event)
    assert answer["statusCode"] == 429
    assert json.loads(answer["body"])["error"]["code"] == "too_many_issuances"


def test_replacement_never_publishes_new_key_before_revocation(table: Any) -> None:
    installation = secrets.token_hex(16)
    old = key_hash(issue_on(table, installation).key)
    observer = boto3.Session().resource("dynamodb").Table(table.name)
    snapshots: list[dict[str, StudentKey]] = []

    def observe(**kwargs: Any) -> None:
        snapshots.append({record.key_hash: record for record in records(observer)})

    table.meta.client.meta.events.register("after-call.dynamodb", observe)
    try:
        new = key_hash(issue_on(table, installation, NOW + 1).key)
    finally:
        table.meta.client.meta.events.unregister("after-call.dynamodb", observe)
    assert snapshots and new in snapshots[-1]
    assert all(new not in state or state[old].revoked_at is not None for state in snapshots)


class OverlappingReads:
    """Pause initial real queries so independent callers can observe the same state.

    The bounded wait also permits implementations that serialize callers before querying.
    Every query and write still goes to moto; no response data is fabricated.
    """

    def __init__(self, table: Any, barrier: threading.Barrier) -> None:
        self.table = table
        self.barrier = barrier
        self.first_query = True

    def query(self, **kwargs: Any) -> Any:
        answer = self.table.query(**kwargs)
        if self.first_query:
            self.first_query = False
            try:
                self.barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
        return answer

    def __getattr__(self, name: str) -> Any:
        return getattr(self.table, name)


def overlapping(table: Any, installations: list[str]) -> list[object]:
    barrier = threading.Barrier(len(installations))
    # Sessions/resources are separate, as they are in independent Lambda workers.
    tables = [boto3.Session().resource("dynamodb").Table(table.name) for _ in installations]

    def login(pair: tuple[Any, str]) -> object:
        worker_table, installation = pair
        try:
            return issue_on(OverlappingReads(worker_table, barrier), installation, NOW + 10)
        except Refused as refused:
            return refused.refusal

    with ThreadPoolExecutor(max_workers=len(installations)) as pool:
        return list(pool.map(login, zip(tables, installations, strict=True)))


def test_overlapping_replacements_leave_one_active_key(table: Any) -> None:
    installation = secrets.token_hex(16)
    issue_on(table, installation)
    overlapping(table, [installation, installation])
    active = [record for record in records(table) if record.installation == installation and record.revoked_at is None]
    assert len(active) == 1, f"replacement left {len(active)} active keys on one installation"


def test_overlapping_logins_cannot_create_three_installations(table: Any) -> None:
    installations = [secrets.token_hex(16) for _ in range(3)]
    overlapping(table, installations)
    active = {record.installation for record in records(table) if record.revoked_at is None}
    assert len(active) <= 2, f"issued active keys to {len(active)} installations"


def test_overlapping_logins_cannot_exceed_daily_limit(table: Any) -> None:
    laptop, desktop = secrets.token_hex(16), secrets.token_hex(16)
    for offset in range(4):
        issue_on(table, laptop, NOW + offset)
    overlapping(table, [laptop, desktop])
    today = [record for record in records(table) if NOW <= record.issued_at < NOW + 86400]
    assert len(today) <= 5, f"issued {len(today)} keys in one UTC day with limit 5"
