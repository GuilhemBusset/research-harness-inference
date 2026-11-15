"""Independent issuer checks with moto and a request-level GitHub stub."""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import logging
import secrets
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.keys import is_valid_key
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import KEYS, ROSTER, Table
from rhi_issuer import handler, redeem

if TYPE_CHECKING:
    from collections.abc import Iterator
    from urllib.request import Request


class Response(io.BytesIO):
    status = 200


@dataclass
class Harness:
    issuer: handler.Issuer
    roster: Any
    keys: Any
    student: RosterEntry
    token: str
    client_id: str
    device_code: str
    installation: str
    clock: list[int]
    calls: list[str]
    user_id: int = 7654
    error: str | None = None
    detail: str = "untrusted upstream text"

    def open(self, request: Request, *, timeout: int) -> Response:
        assert timeout > 0
        self.calls.append(request.full_url)
        if request.full_url == "https://github.com/login/oauth/access_token":
            assert request.get_method() == "POST"
            assert isinstance(request.data, bytes)
            assert urllib.parse.parse_qs(request.data.decode()) == {
                "client_id": [self.client_id],
                "device_code": [self.device_code],
                "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
            }
            payload = (
                {"error": self.error, "error_description": self.detail}
                if self.error
                else {"access_token": self.token, "token_type": "bearer"}
            )
        else:
            assert request.full_url == "https://api.github.com/user"
            assert request.get_method() == "GET"
            assert request.get_header("Authorization") == f"Bearer {self.token}"
            payload = {"id": self.user_id, "login": "a-handle-that-is-not-the-roster-key"}
        return Response(json.dumps(payload).encode())

    def login(self, **changes: object) -> dict[str, Any]:
        body: dict[str, object] = {"device_code": self.device_code, "installation": self.installation}
        body.update(changes)
        return self.issuer.answer({"httpMethod": "POST", "path": "/auth/login", "body": json.dumps(body)})


def create_table(db: Any, schema: Table) -> Any:
    def key(attribute: Any, kind: str) -> dict[str, str]:
        return {"AttributeName": attribute.name, "KeyType": kind}

    attributes = {schema.partition_key}
    for index in schema.indexes:
        attributes.add(index.partition_key)
        if index.sort_key:
            attributes.add(index.sort_key)
    return db.create_table(
        TableName=f"probe-{schema.name}",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": attr.name, "AttributeType": attr.type.value} for attr in attributes],
        KeySchema=[key(schema.partition_key, "HASH")],
        GlobalSecondaryIndexes=[
            {
                "IndexName": index.name,
                "KeySchema": [key(index.partition_key, "HASH")]
                + ([key(index.sort_key, "RANGE")] if index.sort_key else []),
                "Projection": {"ProjectionType": "ALL"},
            }
            for index in schema.indexes
        ],
    )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        db = boto3.resource("dynamodb")
        roster, keys = create_table(db, ROSTER), create_table(db, KEYS)
        clock = [1_800_001_234]
        student = RosterEntry(7654, "lynx", 987654, 4, clock[0] - 100, 2_000_000_000)
        roster.put_item(Item=student.to_item())
        client_id = "Iv1." + secrets.token_hex(8)
        for name, value in {
            "RHI_CLIENT_ID": client_id,
            "RHI_COURSE_END": "2028-02-29",
            "RHI_ROSTER_TABLE": roster.name,
            "RHI_KEYS_TABLE": keys.name,
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(handler, "_dynamodb", lambda: db)
        result = Harness(
            handler.from_environment(clock=lambda: clock[0]), roster, keys, student,
            secrets.token_urlsafe(32), client_id, secrets.token_hex(20), secrets.token_hex(16), clock, [],
        )
        monkeypatch.setattr(redeem._OPENER, "open", result.open)
        yield result


def test_member_storage_limits_and_previous_time(
    harness: Harness, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    first = harness.login()
    assert first["statusCode"] == 200
    body = json.loads(first["body"])
    assert body["previous_issued_at"] is None
    assert body["issued_at"] == harness.clock[0]
    key = body["key"]
    assert is_valid_key(key)
    digest = hashlib.sha256(key.encode()).hexdigest()
    item = harness.keys.get_item(Key={"key_hash": digest})["Item"]
    record = StudentKey.from_item(item)
    assert (record.alias, record.daily_token_budget, record.concurrency_limit) == ("lynx", 987654, 4)
    assert record.github_id == harness.student.github_id
    assert record.installation == harness.installation
    assert record.issued_at == body["issued_at"]
    expected_expiry = int(datetime.datetime(2028, 3, 1, tzinfo=datetime.UTC).timestamp())
    assert record.expires_at == body["expires_at"] == expected_expiry
    assert record.key_hash == digest
    assert len(harness.calls) == 2
    stored = repr([harness.keys.scan()["Items"], harness.roster.scan()["Items"]])
    output = capfd.readouterr()
    for secret in (key, harness.token):
        assert secret not in stored + caplog.text + output.out + output.err
    assert harness.token not in first["body"]
    harness.clock[0] += 123
    second = json.loads(harness.login()["body"])
    assert second["previous_issued_at"] == body["issued_at"]
    assert second["key"] != key
    assert len(harness.keys.scan()["Items"]) == 2


@pytest.mark.parametrize("code", ["authorization_pending", "slow_down", "expired_token"])
def test_fixed_polling_codes(harness: Harness, code: str) -> None:
    harness.error = code
    first = harness.login()
    harness.detail = secrets.token_hex(16)
    second = harness.login()
    assert first == second
    assert first["statusCode"] == 400
    assert json.loads(first["body"])["error"]["code"] == code
    assert len(harness.calls) == 2
    assert harness.keys.scan()["Items"] == []


@pytest.mark.parametrize(
    "changes",
    [{"device_code": "bad"}, {"device_code": None}, {"installation": "bad"}, {"installation": ["bad"]}],
)
def test_shape_checks_precede_github(harness: Harness, changes: dict[str, object]) -> None:
    answer = harness.login(**changes)
    assert answer["statusCode"] == 400
    assert json.loads(answer["body"])["error"]["code"] == "invalid_request"
    assert harness.calls == []
    assert harness.keys.scan()["Items"] == []



def test_outsiders_receive_identical_refusal(harness: Harness) -> None:
    harness.user_id += 1
    first = harness.login()
    harness.user_id += 1
    second = harness.login()
    assert first == second
    assert first["statusCode"] == 403
    assert json.loads(first["body"])["error"]["code"] == "not_enrolled"
    assert harness.student.alias not in first["body"]
    assert harness.keys.scan()["Items"] == []



def test_entry_point_routes(harness: Harness) -> None:
    answer = handler.handler({"httpMethod": "GET", "path": "/auth/config"}, None)
    assert answer["statusCode"] == 200
    assert json.loads(answer["body"]) == {"client_id": harness.client_id}
    assert harness.calls == []
    answer = handler.handler(
        {"httpMethod": "POST", "path": "/auth/login", "body": json.dumps({
            "device_code": harness.device_code, "installation": harness.installation,
        })},
        None,
    )
    assert answer["statusCode"] == 200
    assert is_valid_key(json.loads(answer["body"])["key"])
