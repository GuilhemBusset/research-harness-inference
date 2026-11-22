"""The rules of an issuance, against a keys table that moto creates with the schema of `rhi_common.tables`.

Every key is made by the code under test when the tests run: none is written in this file.
"""

from __future__ import annotations

import datetime
import secrets
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.keys import key_hash
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import KEYS
from rhi_issuer import handler
from rhi_issuer.mint import expiry
from rhi_issuer.redeem import Refusal, Refused
from rhi_issuer.rules import issue

if TYPE_CHECKING:
    from collections.abc import Iterator

COURSE_END = datetime.date(2027, 6, 30)
# 08:00 UTC: the day has started 8 hours ago and ends in 16.
NOW = 1_800_000_000
DAY_END = NOW - NOW % 86400 + 86400
STUDENT = RosterEntry(
    github_id=4242,
    alias="otter",
    concurrency_limit=3,
    added_at=NOW - 86400,
    expires_at=expiry(COURSE_END) + 86400,
)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    index = KEYS.index("by_student")
    with mock_aws():
        yield boto3.resource("dynamodb").create_table(
            TableName=f"keys-{secrets.token_hex(4)}",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": attribute.name, "AttributeType": attribute.type.value}
                for attribute in (KEYS.partition_key, index.partition_key, index.sort_key)
                if attribute is not None
            ],
            KeySchema=[{"AttributeName": KEYS.partition_key.name, "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": index.name,
                    "KeySchema": [
                        {"AttributeName": index.partition_key.name, "KeyType": "HASH"},
                        {"AttributeName": "issued_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )


def login(keys: Any, installation: str, now: int = NOW, **limit: int) -> str:
    return issue(STUDENT, installation, course_end=COURSE_END, now=now, keys=keys, **limit).key


def stored(keys: Any) -> list[StudentKey]:
    return [StudentKey.from_item(item) for item in keys.scan()["Items"]]


def active(keys: Any, installation: str) -> list[str]:
    return [record.key_hash for record in stored(keys) if record.installation == installation and not record.revoked_at]


def test_a_second_login_from_the_same_installation_leaves_it_one_active_key(keys: Any) -> None:
    laptop = secrets.token_hex(16)
    first = login(keys, laptop)
    second = login(keys, laptop, NOW + 60)

    assert first != second
    assert active(keys, laptop) == [key_hash(second)]
    assert all("daily_token_budget" not in item for item in keys.scan()["Items"])
    (old,) = [record for record in stored(keys) if record.key_hash == key_hash(first)]
    assert old.revoked_at == NOW + 60


def test_a_second_installation_gets_its_own_key_and_a_third_is_refused(keys: Any) -> None:
    laptop, desktop, third = (secrets.token_hex(16) for _ in range(3))
    laptop_key = login(keys, laptop)
    desktop_key = login(keys, desktop)

    assert active(keys, laptop) == [key_hash(laptop_key)]
    assert active(keys, desktop) == [key_hash(desktop_key)]

    before = stored(keys)
    with pytest.raises(Refused) as refused:
        login(keys, third)
    assert refused.value.refusal is Refusal.TOO_MANY_INSTALLATIONS
    assert "rhi revoke --all" in Refusal.TOO_MANY_INSTALLATIONS.sentence
    assert stored(keys) == before

    # Either installation still logs in, and replaces its own key.
    login(keys, desktop)
    assert len(active(keys, desktop)) == 1
    # Once the student's keys are revoked, as `rhi revoke --all` does, the third installation logs in.
    for record in stored(keys):
        keys.update_item(
            Key={"key_hash": record.key_hash},
            UpdateExpression="SET revoked_at = :t",
            ExpressionAttributeValues={":t": NOW},
        )
    assert active(keys, third) == []
    login(keys, third)
    assert len(active(keys, third)) == 1


def test_a_sixth_issuance_in_one_utc_day_is_refused(keys: Any) -> None:
    laptop, desktop = secrets.token_hex(16), secrets.token_hex(16)
    for n, installation in enumerate([laptop, desktop, laptop, desktop, laptop]):
        login(keys, installation, NOW + n)

    before = stored(keys)
    with pytest.raises(Refused) as refused:
        login(keys, desktop, DAY_END - 1)
    assert refused.value.refusal is Refusal.TOO_MANY_ISSUANCES
    assert stored(keys) == before

    # The count is over the UTC day: at midnight the student logs in again.
    login(keys, desktop, DAY_END)
    assert len(active(keys, desktop)) == 1


def test_the_daily_limit_is_the_number_given(keys: Any) -> None:
    laptop = secrets.token_hex(16)
    login(keys, laptop, issuances_per_day=2)
    login(keys, laptop, issuances_per_day=2)
    with pytest.raises(Refused) as refused:
        login(keys, laptop, issuances_per_day=2)
    assert refused.value.refusal is Refusal.TOO_MANY_ISSUANCES


def test_the_function_reads_the_limit_from_its_environment(keys: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "RHI_CLIENT_ID": "Iv1." + secrets.token_hex(8),
        "RHI_COURSE_END": COURSE_END.isoformat(),
        "RHI_ROSTER_TABLE": "roster",
        "RHI_KEYS_TABLE": keys.name,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("RHI_ISSUANCES_PER_DAY", raising=False)
    assert handler.from_environment().issuances_per_day == 5
    monkeypatch.setenv("RHI_ISSUANCES_PER_DAY", "3")
    assert handler.from_environment().issuances_per_day == 3


def test_replacement_is_one_write(keys: Any) -> None:
    laptop, desktop = secrets.token_hex(16), secrets.token_hex(16)
    old = key_hash(login(keys, laptop))
    login(keys, desktop)

    # After every call the issuer makes, the whole store, as another client reads it.
    observer = boto3.Session().resource("dynamodb").Table(keys.name)
    calls: list[str] = []
    states: list[dict[str, StudentKey]] = []

    def snapshot(event_name: str, **_: object) -> None:
        calls.append(event_name.rsplit(".", 1)[-1])
        states.append({item["key_hash"]: StudentKey.from_item(item) for item in observer.scan()["Items"]})

    keys.meta.client.meta.events.register("after-call.dynamodb", snapshot)
    new = key_hash(login(keys, laptop, NOW + 60))
    keys.meta.client.meta.events.unregister("after-call.dynamodb", snapshot)

    assert new in states[-1]
    for state in states:
        assert new not in state or state[old].revoked_at is not None
    assert [call for call in calls if call != "Query"] == ["TransactWriteItems"]
