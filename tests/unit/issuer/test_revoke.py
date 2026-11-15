"""Revoking keys through the issuer's two routes, against a stub of GitHub and tables that moto creates.

Every key comes from a login through the handler, or from `generate_key`, and every device code
and token from `secrets`, when the tests run: none is written in this file.
"""

from __future__ import annotations

import dataclasses
import datetime
import http.server
import json
import secrets
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.keys import generate_key, key_hash
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import KEYS, ROSTER, KeyAttribute, Table
from rhi_issuer import handler
from rhi_issuer.redeem import GitHub, Refusal

if TYPE_CHECKING:
    from collections.abc import Iterator

NOW = 1_800_000_000
CLIENT_ID = "Iv1." + secrets.token_hex(8)
OTTER = RosterEntry(
    github_id=4242,
    alias="otter",
    daily_token_budget=3_000_000,
    concurrency_limit=3,
    added_at=NOW - 86400,
    expires_at=NOW + 90 * 86400,
)
HERON = dataclasses.replace(OTTER, github_id=5151, alias="heron")
OUTSIDER = 99


class Stub(http.server.BaseHTTPRequestHandler):
    """GitHub's device flow: each device code names the user who confirmed it."""

    codes: dict[str, int]
    tokens: dict[str, int]
    calls: list[str]

    def do_POST(self) -> None:
        self.calls.append(self.path)
        if self.path == "/token":
            form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            user = self.codes.get(form["device_code"])
            token = "ghu_" + secrets.token_urlsafe(27)
            if user is not None:
                self.tokens[token] = user
            answer = {"access_token": token} if user is not None else {"error": "incorrect_device_code"}
        else:
            answer = {"id": self.tokens[(self.headers["Authorization"] or "").removeprefix("Bearer ")]}
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST

    def log_message(self, format: str, *args: object) -> None:
        pass


def _schema(*attributes: KeyAttribute | None) -> list[dict[str, str]]:
    present = [attribute for attribute in attributes if attribute is not None]
    return [{"AttributeName": a.name, "KeyType": kind} for a, kind in zip(present, ("HASH", "RANGE"), strict=False)]


def _create(dynamodb: Any, table: Table) -> Any:
    keyed = {table.partition_key, table.sort_key} | {
        key for index in table.indexes for key in (index.partition_key, index.sort_key)
    }
    return dynamodb.create_table(
        TableName=f"{table.name}-{secrets.token_hex(4)}",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": k.name, "AttributeType": k.type.value} for k in keyed if k is not None],
        KeySchema=_schema(table.partition_key, table.sort_key),
        GlobalSecondaryIndexes=[
            {
                "IndexName": i.name,
                "KeySchema": _schema(i.partition_key, i.sort_key),
                "Projection": {"ProjectionType": "ALL"},
            }
            for i in table.indexes
        ],
    )


class Env:
    """The issuer, its two tables and the stub's record of calls."""

    def __init__(self, issuer: handler.Issuer, codes: dict[str, int], calls: list[str]) -> None:
        self.issuer, self.codes, self.calls = issuer, codes, calls

    def device_code(self, user: int) -> str:
        code = secrets.token_hex(20)
        self.codes[code] = user
        return code

    def post(self, path: str, body: object = None, *, authorization: str | None = None) -> tuple[int, Any]:
        headers = {"content-type": "application/json", "host": "example.com"}
        if authorization is not None:
            headers["authorization"] = authorization
        text = "" if body is None else json.dumps(body)
        event = {"httpMethod": "POST", "path": path, "headers": headers, "body": text, "isBase64Encoded": False}
        answer = self.issuer.answer(event)
        return answer["statusCode"], json.loads(answer["body"])

    def login(self, user: int = OTTER.github_id) -> tuple[int, Any]:
        return self.post("/auth/login", {"device_code": self.device_code(user), "installation": secrets.token_hex(16)})

    def key(self, user: int = OTTER.github_id) -> str:
        status, answer = self.login(user)
        assert status == 200
        return answer["key"]

    def revoked_at(self, key: str) -> int | None:
        return StudentKey.from_item(self.issuer.keys.get_item(Key={"key_hash": key_hash(key)})["Item"]).revoked_at

    def items(self) -> list[dict[str, Any]]:
        return self.issuer.keys.scan()["Items"]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    codes: dict[str, int] = {}
    calls: list[str] = []
    server_class = type("Server", (Stub,), {"codes": codes, "tokens": {}, "calls": calls})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server_class)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        roster, keys = _create(dynamodb, ROSTER), _create(dynamodb, KEYS)
        for entry in (OTTER, HERON):
            roster.put_item(Item=entry.to_item())
        issuer = handler.Issuer(
            client_id=CLIENT_ID,
            course_end=datetime.date(2027, 6, 30),
            roster=roster,
            keys=keys,
            github=GitHub(token_url=url + "/token", user_url=url + "/user"),
            clock=lambda: NOW,
        )
        yield Env(issuer, codes, calls)
    server.shutdown()
    server.server_close()


def refused(refusal: Refusal) -> tuple[int, dict[str, dict[str, str]]]:
    return refusal.status, {"error": {"code": refusal.value, "message": refusal.sentence}}


def test_a_key_revokes_itself_only(env: Env) -> None:
    first, second, other = env.key(), env.key(), env.key(HERON.github_id)

    assert env.post("/auth/revoke", authorization=f"Bearer {first}") == (200, {"revoked": 1})

    assert env.revoked_at(first) == NOW
    # The student's other key, and another student's, are still active.
    assert env.revoked_at(second) is None
    assert env.revoked_at(other) is None


def test_a_key_that_cannot_revoke_is_refused_and_changes_nothing(env: Env) -> None:
    revoked, active = env.key(), env.key()
    assert env.post("/auth/revoke", authorization=f"Bearer {revoked}")[0] == 200
    before = env.items()
    unknown = generate_key()
    # The last character of the checksum changed: the shape of a key, not its checksum.
    malformed = unknown[:-1] + ("A" if unknown[-1] != "A" else "B")
    for authorization in (f"Bearer {revoked}", f"Bearer {unknown}", f"Bearer {malformed}", active, None):
        assert env.post("/auth/revoke", authorization=authorization) == refused(Refusal.INVALID_KEY)
    # A key in the body is not one in the Authorization header.
    assert env.post("/auth/revoke", {"key": active}) == refused(Refusal.INVALID_KEY)
    assert env.items() == before
    assert env.revoked_at(active) is None


def test_revoking_all_needs_a_device_flow(env: Env) -> None:
    keys = [env.key(), env.key()]
    other = env.key(HERON.github_id)
    # A third installation is refused while two hold active keys.
    assert env.login() == refused(Refusal.TOO_MANY_INSTALLATIONS)

    # A key, in the header or in the body, does not stand in for the device code: nothing is revoked.
    calls = len(env.calls)
    for body in (None, {"key": keys[0]}, {"device_code": keys[0]}):
        assert env.post("/auth/revoke-all", body, authorization=f"Bearer {keys[0]}") == refused(Refusal.INVALID_REQUEST)
    assert len(env.calls) == calls
    assert [env.revoked_at(key) for key in keys] == [None, None]

    # A redeemed device code, and no key presented, revokes every key of the student.
    status, answer = env.post("/auth/revoke-all", {"device_code": env.device_code(OTTER.github_id)})
    assert (status, answer) == (200, {"revoked": 2})
    assert [env.revoked_at(key) for key in keys] == [NOW, NOW]
    assert env.revoked_at(other) is None

    # A third installation is accepted afterwards.
    status, answer = env.login()
    assert status == 200
    assert env.revoked_at(answer["key"]) is None


def test_revoking_all_checks_the_roster_and_the_code(env: Env) -> None:
    key = env.key()
    before = env.items()
    assert env.post("/auth/revoke-all", {"device_code": env.device_code(OUTSIDER)}) == refused(Refusal.NOT_ENROLLED)
    assert env.post("/auth/revoke-all", {"device_code": secrets.token_hex(20)}) == refused(Refusal.LOGIN_REFUSED)
    assert env.items() == before
    assert env.revoked_at(key) is None
