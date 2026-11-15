"""Independent evidence for the issuer's self and device-flow revocations."""

from __future__ import annotations

import datetime
import json
import secrets
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import rhi_issuer.redeem as redemption
from rhi_common.keys import generate_key, key_hash
from rhi_common.students import RosterEntry
from rhi_issuer.handler import Issuer

NOW = 1_800_000_000
STUDENT = 4321
OTHER = 5432


@pytest.fixture
def issuer_flow(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Issuer, dict[str, int]]]:
    """Use real issuer and DynamoDB behavior; replace only GitHub's HTTP transport."""
    codes: dict[str, int] = {}
    tokens: dict[str, int] = {}
    client_id = secrets.token_hex(12)

    def github_call(url: str, *, form: dict[str, str] | None = None, bearer: str | None = None) -> tuple[int, Any]:
        if form is not None:
            assert form["client_id"] == client_id
            assert form["grant_type"] == redemption.GRANT_TYPE
            user = codes.pop(form["device_code"], None)
            if user is None:
                return 200, {"error": "incorrect_device_code"}
            token = secrets.token_urlsafe(24)
            tokens[token] = user
            return 200, {"access_token": token}
        assert bearer is not None
        return 200, {"id": tokens.pop(bearer)}

    monkeypatch.setattr(redemption, "_call", github_call)
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        keys = dynamodb.create_table(
            TableName="probe-keys",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "key_hash", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "key_hash", "AttributeType": "S"},
                {"AttributeName": "github_id", "AttributeType": "N"},
                {"AttributeName": "issued_at", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "by_student",
                "KeySchema": [
                    {"AttributeName": "github_id", "KeyType": "HASH"},
                    {"AttributeName": "issued_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        roster = dynamodb.create_table(
            TableName="probe-roster",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "github_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "github_id", "AttributeType": "N"}],
        )
        for user in (STUDENT, OTHER):
            roster.put_item(Item=RosterEntry(user, "student", 1000, 2, NOW - 100, NOW + 86400).to_item())
        yield Issuer(client_id, datetime.date(2027, 6, 30), roster, keys, clock=lambda: NOW), codes


def post(issuer: Issuer, path: str, body: object = None, key: str | None = None) -> tuple[int, Any]:
    headers = {} if key is None else {"Authorization": f"Bearer {key}"}
    response = issuer.answer({"httpMethod": "POST", "path": path, "headers": headers, "body": json.dumps(body)})
    return response["statusCode"], json.loads(response["body"])


def code_for(codes: dict[str, int], user: int = STUDENT) -> str:
    code = secrets.token_hex(20)
    codes[code] = user
    return code


def login(issuer: Issuer, codes: dict[str, int], user: int = STUDENT) -> tuple[int, Any]:
    return post(issuer, "/auth/login", {"device_code": code_for(codes, user), "installation": secrets.token_hex(16)})


def new_key(issuer: Issuer, codes: dict[str, int], user: int = STUDENT) -> str:
    status, body = login(issuer, codes, user)
    assert status == 200
    return body["key"]


def items(issuer: Issuer) -> dict[str, Any]:
    return {item["key_hash"]: item for item in issuer.keys.scan(ConsistentRead=True)["Items"]}


def test_self_revoke_changes_only_the_bearer_item(issuer_flow: tuple[Issuer, dict[str, int]]) -> None:
    issuer, codes = issuer_flow
    first, second = new_key(issuer, codes), new_key(issuer, codes)
    new_key(issuer, codes, OTHER)
    before = items(issuer)
    assert post(issuer, "/auth/revoke", key=first)[0] == 200
    expected = {hashed: dict(item) for hashed, item in before.items()}
    expected[key_hash(first)]["revoked_at"] = NOW
    assert items(issuer) == expected
    assert "revoked_at" not in items(issuer)[key_hash(second)]


def test_refusals_are_fixed_and_each_revocation_is_one_write(issuer_flow: tuple[Issuer, dict[str, int]]) -> None:
    issuer, codes = issuer_flow
    key = new_key(issuer, codes)
    with patch.object(issuer.keys, "update_item", wraps=issuer.keys.update_item) as update:
        assert post(issuer, "/auth/revoke", key=key)[0] == 200
        assert update.call_count == 1
    before = items(issuer)
    unknown = generate_key()
    malformed = unknown[:-1] + ("A" if unknown[-1] != "A" else "B")
    responses = [post(issuer, "/auth/revoke", key=value) for value in (None, malformed, unknown, key)]
    assert all(response == responses[0] for response in responses)
    assert responses[0][0] == 401
    assert isinstance(responses[0][1]["error"]["message"], str)
    assert items(issuer) == before


def test_device_redemption_revokes_all_and_frees_an_installation(issuer_flow: tuple[Issuer, dict[str, int]]) -> None:
    issuer, codes = issuer_flow
    first, second = new_key(issuer, codes), new_key(issuer, codes)
    other = new_key(issuer, codes, OTHER)
    assert login(issuer, codes)[0] == 403
    before = items(issuer)
    for body in ({"key": first}, {"device_code": first}, None):
        assert post(issuer, "/auth/revoke-all", body, key=first)[0] == 400
        assert items(issuer) == before
    assert post(issuer, "/auth/revoke-all", {"device_code": code_for(codes, 9999)})[0] == 403
    assert items(issuer) == before
    device_code = code_for(codes)
    with patch.object(issuer.keys, "update_item", wraps=issuer.keys.update_item) as update:
        assert post(issuer, "/auth/revoke-all", {"device_code": device_code})[0] == 200
        assert update.call_count == 2
    after = items(issuer)
    assert after[key_hash(first)]["revoked_at"] == NOW
    assert after[key_hash(second)]["revoked_at"] == NOW
    assert after[key_hash(other)] == before[key_hash(other)]
    snapshot = items(issuer)
    assert post(issuer, "/auth/revoke-all", {"device_code": device_code})[0] != 200
    assert items(issuer) == snapshot
    third = new_key(issuer, codes)
    assert "revoked_at" not in items(issuer)[key_hash(third)]
