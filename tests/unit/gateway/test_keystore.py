"""The lookup of a key (`rhi_gateway.keystore`): the DynamoDB store against moto, beside the in-memory one.

moto evaluates DynamoDB's key schemas in process: no account, no credentials, no network. Every
key is generated when the tests run: no key is written in this file.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import boto3
import pytest
from moto import mock_aws
from starlette.datastructures import Headers

from rhi_common.keys import generate_key, key_hash
from rhi_common.students import StudentKey
from rhi_common.tables import KEYS, KeyAttribute
from rhi_gateway.auth import Authenticator
from rhi_gateway.errors import GatewayError
from rhi_gateway.keystore import DynamoKeyStore, InMemoryKeyStore, KeyRecord, KeyStore

NOW = 1_800_000_000
COURSE_END = NOW + 90 * 86400


def issued(key: str, *, expires_at: int = COURSE_END, revoked_at: int | None = None) -> StudentKey:
    return StudentKey(
        key_hash=key_hash(key),
        github_id=4242,
        alias="otter",
        installation="install-1",
        issued_at=NOW - 3600,
        expires_at=expires_at,
        revoked_at=revoked_at,
        daily_token_budget=2_000_000,
        concurrency_limit=4,
    )


# What a store answers for a key of `issued`, as it is issued.
RECORD = KeyRecord(
    github_id=4242,
    alias="otter",
    daily_token_budget=2_000_000,
    concurrency_limit=4,
    expires_at=COURSE_END,
    revoked=False,
)


def _schema(*attributes: KeyAttribute | None) -> list[dict[str, str]]:
    """A key schema in DynamoDB's form: the partition key, then the sort key when there is one."""
    present = [attribute for attribute in attributes if attribute is not None]
    return [
        {"AttributeName": attribute.name, "KeyType": kind}
        for attribute, kind in zip(present, ("HASH", "RANGE"), strict=False)
    ]


@pytest.fixture
def table() -> Iterator[Any]:
    """The keys table, with the key schema and the index of `rhi_common.tables.KEYS`, in moto."""
    with mock_aws():
        keyed = {KEYS.partition_key, *(key for index in KEYS.indexes for key in (index.partition_key, index.sort_key))}
        yield boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="keys-under-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": key.name, "AttributeType": key.type.value} for key in keyed if key is not None
            ],
            KeySchema=_schema(KEYS.partition_key, KEYS.sort_key),
            GlobalSecondaryIndexes=[
                {
                    "IndexName": index.name,
                    "KeySchema": _schema(index.partition_key, index.sort_key),
                    "Projection": {"ProjectionType": "ALL"},
                }
                for index in KEYS.indexes
            ],
        )


def test_the_table_has_the_key_schema_of_the_key_item(table: Any) -> None:
    assert table.key_schema == [{"AttributeName": "key_hash", "KeyType": "HASH"}]
    (index,) = table.global_secondary_indexes
    assert index["IndexName"] == "by_student"
    assert index["KeySchema"] == [
        {"AttributeName": "github_id", "KeyType": "HASH"},
        {"AttributeName": "issued_at", "KeyType": "RANGE"},
    ]


def test_a_key_item_is_read_back(table: Any) -> None:
    key = generate_key()
    table.put_item(Item=issued(key).to_item())

    assert DynamoKeyStore(table).lookup(key_hash(key)) == RECORD


def test_a_lookup_is_one_read_of_the_keys_table(table: Any) -> None:
    key = generate_key()
    table.put_item(Item=issued(key).to_item())
    calls: list[tuple[str, str]] = []

    def record(params: dict[str, Any], model: Any, **_: object) -> None:
        calls.append((model.name, params["TableName"]))

    table.meta.client.meta.events.register("provide-client-params.dynamodb", record)
    DynamoKeyStore(table).lookup(key_hash(key))

    assert calls == [("GetItem", table.name)]


def test_both_stores_give_the_same_answers(table: Any) -> None:
    active, unknown, revoked, expired = (generate_key() for _ in range(4))
    items = [issued(active), issued(revoked, revoked_at=NOW - 60), issued(expired, expires_at=NOW - 1)]
    for item in items:
        table.put_item(Item=item.to_item())
    stores: tuple[KeyStore, KeyStore] = (DynamoKeyStore(table), InMemoryKeyStore(*items))

    def accepted(store: KeyStore, key: str) -> bool:
        try:
            asyncio.run(Authenticator(store, wall_clock=lambda: NOW).authenticate(Headers({"x-api-key": key})))
        except GatewayError:
            return False
        return True

    for key, record in (
        (active, RECORD),
        (unknown, None),
        (revoked, replace(RECORD, revoked=True)),
        (expired, replace(RECORD, expires_at=NOW - 1)),
    ):
        assert [store.lookup(key_hash(key)) for store in stores] == [record, record]
        assert [accepted(store, key) for store in stores] == [key is active, key is active]


def test_an_item_that_is_not_a_key_item_gives_no_record(table: Any) -> None:
    key = generate_key()
    item = issued(key).to_item()
    del item["alias"]
    table.put_item(Item=item)

    assert DynamoKeyStore(table).lookup(key_hash(key)) is None
