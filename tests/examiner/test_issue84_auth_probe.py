"""Independent issue 84 checks of header admission, cache bounds and key-table lookup."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import boto3
import httpx
import pytest
from moto import mock_aws
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response

from rhi_common.causes import Cause
from rhi_common.keys import KEY_PATTERN, generate_key, key_hash
from rhi_common.students import StudentKey
from rhi_gateway.app import build_app
from rhi_gateway.auth import Authenticator, authenticated
from rhi_gateway.errors import GatewayError
from rhi_gateway.keystore import DynamoKeyStore, InMemoryKeyStore, KeyRecord
from rhi_gateway.routes import MESSAGES, Route

EPOCH = 1_800_000_000


def item(key: str) -> StudentKey:
    return StudentKey(key_hash(key), 73, "fox", None, EPOCH - 10, EPOCH + 900, None, 12000, 3)


@pytest.mark.parametrize("kind", ["bearer", "api", "both"])
def test_headers_return_the_complete_identity(kind: str) -> None:
    key = generate_key()
    store = InMemoryKeyStore(item(key))
    auth = Authenticator(store, wall_clock=lambda: EPOCH)
    headers = {}
    if kind in ("bearer", "both"):
        headers["authorization"] = "Bearer " + key
    if kind in ("api", "both"):
        headers["x-api-key"] = key
    result = asyncio.run(auth.authenticate(Headers(headers)))
    assert result == KeyRecord(73, "fox", 12000, 3, EPOCH + 900, False)
    assert store.lookups == [key_hash(key)]


@pytest.mark.parametrize("reason", ["missing", "conflicting", "shape", "checksum", "unknown", "revoked", "expired"])
def test_refusals_do_not_read_body_or_enter_budget_handler(reason: str) -> None:
    key, other = generate_key(), generate_key()
    record = item(key)
    if reason == "revoked":
        record = replace(record, revoked_at=EPOCH - 1)
    if reason == "expired":
        record = replace(record, expires_at=EPOCH - 1)
    store = InMemoryKeyStore(record)
    auth = Authenticator(store, wall_clock=lambda: EPOCH)
    headers = {"x-api-key": key}
    if reason == "missing":
        headers = {}
    elif reason == "conflicting":
        headers["authorization"] = "Bearer " + other
    elif reason == "shape":
        headers["x-api-key"] = key[:-1]
    elif reason == "checksum":
        headers["x-api-key"] = key[:-1] + ("0" if key[-1] != "0" else "1")
    elif reason == "unknown":
        headers["x-api-key"] = other

    async def forbidden_handler(request: Request, route: Route, student: KeyRecord) -> Response:
        raise AssertionError("refused key entered the body/budget handler")

    async def unreadable() -> AsyncIterator[bytes]:
        raise AssertionError("refused request read its body")
        yield b""

    async def exercise() -> None:
        app = build_app({MESSAGES: authenticated(auth, forbidden_handler)})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.com") as client:
            response = await client.post(MESSAGES.path, headers=headers, content=unreadable())
        assert response.status_code == 401
        assert response.json()["error"] == {"type": "invalid_key", "message": Cause.INVALID_KEY.sentence}

    asyncio.run(exercise())
    assert store.lookups == (
        [] if reason in {"missing", "conflicting", "shape", "checksum"} else [key_hash(headers["x-api-key"])]
    )


def test_body_and_budget_handler_run_after_acceptance() -> None:
    key = generate_key()
    store = InMemoryKeyStore(item(key))
    auth = Authenticator(store, wall_clock=lambda: EPOCH)
    actions = []

    async def downstream(request: Request, route: Route, student: KeyRecord) -> Response:
        assert store.lookups == [key_hash(key)]
        assert student.github_id == 73
        actions.append("budget")
        assert await request.body() == b"{}"
        return Response(status_code=204)

    async def exercise() -> None:
        app = build_app({MESSAGES: authenticated(auth, downstream)})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://example.com") as client:
            answer = await client.post(MESSAGES.path, headers={"x-api-key": key}, content=b"{}")
        assert answer.status_code == 204

    asyncio.run(exercise())
    assert actions == ["budget"]


@pytest.mark.parametrize("found", [True, False])
def test_positive_and_negative_answers_are_cached_for_60_seconds(found: bool) -> None:
    key = generate_key()
    now = [0.0]
    store = InMemoryKeyStore(*([item(key)] if found else []))
    auth = Authenticator(store, wall_clock=lambda: EPOCH, monotonic=lambda: now[0])

    async def request() -> None:
        if found:
            await auth.authenticate(Headers({"x-api-key": key}))
        else:
            with pytest.raises(GatewayError) as refused:
                await auth.authenticate(Headers({"x-api-key": key}))
            assert refused.value.cause is Cause.INVALID_KEY

    for second, expected_reads in [(0, 1), (59, 1), (61, 2)]:
        now[0] = second
        asyncio.run(request())
        assert store.lookups == [key_hash(key)] * expected_reads


@pytest.mark.parametrize("change", ["revocation", "expiry"])
def test_cached_identity_cannot_outlive_revocation_or_expiry(change: str) -> None:
    key = generate_key()
    now = [float(EPOCH)]
    record = item(key) if change == "revocation" else replace(item(key), expires_at=EPOCH + 30)
    store = InMemoryKeyStore(record)
    auth = Authenticator(store, wall_clock=lambda: now[0], monotonic=lambda: now[0])
    asyncio.run(auth.authenticate(Headers({"x-api-key": key})))
    if change == "revocation":
        store.revoke(key_hash(key), EPOCH + 1)
        now[0] += 61
    else:
        now[0] += 31
    with pytest.raises(GatewayError) as refused:
        asyncio.run(auth.authenticate(Headers({"x-api-key": key})))
    assert refused.value.cause is Cause.INVALID_KEY
    assert len(store.lookups) == (2 if change == "revocation" else 1)


def test_moto_key_schema_and_both_stores_agree() -> None:
    keys = [generate_key() for _ in range(4)]
    records = [
        item(keys[0]),
        replace(item(keys[1]), revoked_at=EPOCH - 1),
        replace(item(keys[2]), expires_at=EPOCH - 1),
    ]
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="examiner-keys",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "key_hash", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": name, "AttributeType": kind}
                for name, kind in [("key_hash", "S"), ("github_id", "N"), ("issued_at", "N")]
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by_student",
                    "KeySchema": [
                        {"AttributeName": "github_id", "KeyType": "HASH"},
                        {"AttributeName": "issued_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        for record in records:
            table.put_item(Item=record.to_item())
        dynamo, memory = DynamoKeyStore(table), InMemoryKeyStore(*records)
        for index, key in enumerate(keys):
            expected = None if index == 3 else KeyRecord(73, "fox", 12000, 3, records[index].expires_at, index == 1)
            assert dynamo.lookup(key_hash(key)) == memory.lookup(key_hash(key)) == expected


def test_auth_test_sources_contain_no_literal_keys() -> None:
    for filename in ("test_auth.py", "test_keystore.py"):
        source = (Path("tests/unit/gateway") / filename).read_text(encoding="utf-8")
        assert re.search(KEY_PATTERN, source) is None
