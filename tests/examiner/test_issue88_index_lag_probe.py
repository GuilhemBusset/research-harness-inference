"""Issuer rules must hold while a real DynamoDB index is still catching up.

Only the index view is delayed; keys and all writes remain in real moto tables.
AWS documents asynchronous GSI propagation, including empty and partial results.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.tables import KEYS
from rhi_issuer.redeem import Refusal, Refused
from test_issue88_rules_probe import (
    NOW,
    issue_on,
    records,
)
from test_issue88_rules_probe import (
    test_replacement_never_publishes_new_key_before_revocation as atomic_replacement,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    index = KEYS.index("by_student")
    with mock_aws():
        yield boto3.resource("dynamodb").create_table(
            TableName="index-lag-probe-keys",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": attribute.name, "AttributeType": attribute.type.value}
                for attribute in (KEYS.partition_key, index.partition_key, index.sort_key)
                if attribute is not None
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


class DelayedIndex:
    """Withhold selected key items from GSI queries until propagation completes.

    Reads of the base table and every mutation still use moto without modification.
    """

    def __init__(self, real_table: Any, hidden: set[str]) -> None:
        self.real_table = real_table
        self.hidden = hidden

    def query(self, **kwargs: Any) -> Any:
        answer = self.real_table.query(**kwargs)
        if kwargs.get("IndexName"):
            answer["Items"] = [item for item in answer.get("Items", []) if item.get("key_hash") not in self.hidden]
            answer["Count"] = len(answer["Items"])
        return answer

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real_table, name)


def test_replacement_with_unpropagated_first_key(table: Any) -> None:
    installation = secrets.token_hex(16)
    issue_on(table, installation)
    delayed = DelayedIndex(table, {record.key_hash for record in records(table)})
    issue_on(delayed, installation, NOW + 1)
    active = [record for record in records(table) if record.revoked_at is None]
    assert len(active) == 1, "a replacement must revoke the first key even before GSI propagation"


def test_third_installation_with_unpropagated_older_key(table: Any) -> None:
    laptop, desktop, third = (secrets.token_hex(16) for _ in range(3))
    issue_on(table, laptop)
    hidden = {record.key_hash for record in records(table)}
    issue_on(table, desktop, NOW + 1)
    delayed = DelayedIndex(table, hidden)
    try:
        issue_on(delayed, third, NOW + 2)
    except Refused as refused:
        assert refused.refusal is Refusal.TOO_MANY_INSTALLATIONS
        assert "rhi revoke --all" in refused.refusal.sentence
    active = {record.installation for record in records(table) if record.revoked_at is None}
    assert len(active) <= 2, "an index missing an older key must not allow a third installation"


def test_daily_limit_with_unpropagated_older_keys(table: Any) -> None:
    installation = secrets.token_hex(16)
    for offset in range(4):
        issue_on(table, installation, NOW + offset)
    hidden = {record.key_hash for record in records(table)}
    issue_on(table, installation, NOW + 4)
    delayed = DelayedIndex(table, hidden)
    try:
        issue_on(delayed, installation, NOW + 5)
    except Refused as refused:
        assert refused.refusal is Refusal.TOO_MANY_ISSUANCES
    day_start = NOW - NOW % 86400
    issued_today = [record for record in records(table) if day_start <= record.issued_at < day_start + 86400]
    assert len(issued_today) <= 5, "an index missing earlier issuances must not allow a sixth key"


def test_atomic_replacement_against_moto(table: Any) -> None:
    atomic_replacement(table)


def test_sequential_rules_against_moto(table: Any) -> None:
    laptop, desktop, third = (secrets.token_hex(16) for _ in range(3))
    issue_on(table, laptop)
    issue_on(table, laptop, NOW + 1)
    issue_on(table, desktop, NOW + 2)
    active = [record for record in records(table) if record.revoked_at is None]
    assert len(active) == 2
    assert {record.installation for record in active} == {laptop, desktop}
    with pytest.raises(Refused) as refused:
        issue_on(table, third, NOW + 3)
    assert refused.value.refusal is Refusal.TOO_MANY_INSTALLATIONS
    assert "rhi revoke --all" in refused.value.refusal.sentence
    issue_on(table, laptop, NOW + 3)
    issue_on(table, desktop, NOW + 4)
    with pytest.raises(Refused) as refused:
        issue_on(table, laptop, NOW + 5)
    assert refused.value.refusal is Refusal.TOO_MANY_ISSUANCES
