"""The key issuer's login, from the device code to the stored key, against a stub of GitHub and moto.

The stub answers on a loopback port in the shapes of GitHub's device flow, so the issuer's own
HTTP calls reach it, and it records every call. The roster and keys tables are made in moto with
the key schemas of `rhi_common.tables`: no account, no credentials, no network. Every device code,
GitHub token and key is made when the tests run: none is written in this file.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import http.server
import json
import logging
import secrets
import threading
import urllib.parse
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

from rhi_common.keys import is_valid_key, key_hash
from rhi_common.students import RosterEntry, StudentKey
from rhi_common.tables import KEYS, ROSTER, KeyAttribute, Table
from rhi_issuer import handler
from rhi_issuer.redeem import GRANT_TYPE, GitHub, Refusal

if TYPE_CHECKING:
    from collections.abc import Iterator

NOW = 1_800_000_000
COURSE_END = "2027-06-30"
# The key works through the last day of the course: it stops at the first second of the next, in UTC.
EXPIRES_AT = int(datetime.datetime(2027, 7, 1, tzinfo=datetime.UTC).timestamp())
CLIENT_ID = "Iv1." + secrets.token_hex(8)
TOKEN_PATH = "/login/oauth/access_token"
USER_PATH = "/user"
# What the stub says in its own words, which no answer of the issuer's may repeat.
MARKER = "said-by-the-stub " + secrets.token_hex(8)

# Two students whose limits differ from each other and from the default.
OTTER = RosterEntry(
    github_id=4242,
    alias="otter",
    concurrency_limit=3,
    added_at=NOW - 86400,
    expires_at=EXPIRES_AT + 90 * 86400,
)
HERON = dataclasses.replace(OTTER, github_id=5151, alias="heron", concurrency_limit=2)
# What the release before wrote on a roster entry, and this release ignores.
WRITTEN_BEFORE = {HERON.github_id: {"daily_token_budget": 7_000_000}}


class Stub:
    """GitHub, as the device flow sees it: the device codes it gave, and what it was asked."""

    def __init__(self) -> None:
        # A device code, and the ID of the user who confirmed it or the error GitHub answers for it.
        self.codes: dict[str, int | str] = {}
        self.tokens: dict[str, int] = {}
        self.user_status = 200
        self.calls: list[str] = []
        self.client_ids: list[str] = []
        self.now = NOW
        self.url = ""

    def device_code(self, outcome: int | str) -> str:
        code = secrets.token_hex(20)
        self.codes[code] = outcome
        return code

    def answer(self, path: str, form: dict[str, str], bearer: str) -> tuple[int, Any]:
        self.calls.append(path)
        if path == TOKEN_PATH:
            self.client_ids.append(form.get("client_id", ""))
            outcome = self.codes.get(form.get("device_code", ""), "incorrect_device_code")
            if form.get("client_id") != CLIENT_ID:
                outcome = "incorrect_client_credentials"
            elif form.get("grant_type") != GRANT_TYPE:
                outcome = "unsupported_grant_type"
            if isinstance(outcome, str):
                return 200, {"error": outcome, "error_description": MARKER, "error_uri": "https://example.com/docs"}
            token = "ghu_" + secrets.token_urlsafe(27)
            self.tokens[token] = outcome
            return 200, {"access_token": token, "token_type": "bearer", "scope": ""}
        if path == USER_PATH and bearer in self.tokens and self.user_status == 200:
            return 200, {"login": f"user-{self.tokens[bearer]}", "id": self.tokens[bearer], "type": "User"}
        return self.user_status if path == USER_PATH else 404, {"message": MARKER}


class Server(http.server.BaseHTTPRequestHandler):
    stub: Stub

    def do_GET(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
        bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        status, answer = self.stub.answer(self.path, form, bearer)
        data = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_GET

    def log_message(self, format: str, *args: object) -> None:  # the server's own access log
        pass


@pytest.fixture
def stub(caplog: pytest.LogCaptureFixture) -> Iterator[Stub]:
    caplog.set_level(logging.DEBUG)
    stub = Stub()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("Server", (Server,), {"stub": stub}))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    stub.url = f"http://127.0.0.1:{server.server_port}"
    yield stub
    server.shutdown()
    server.server_close()
    # Whatever the test did, the issuer never sent GitHub another client ID than the one it was given.
    assert set(stub.client_ids) <= {CLIENT_ID}


def _schema(*attributes: KeyAttribute | None) -> list[dict[str, str]]:
    present = [attribute for attribute in attributes if attribute is not None]
    return [
        {"AttributeName": attribute.name, "KeyType": kind}
        for attribute, kind in zip(present, ("HASH", "RANGE"), strict=False)
    ]


def _create(dynamodb: Any, table: Table) -> Any:
    """A table with the keys and the indexes `rhi_common.tables` gives it, under a name of the test's."""
    keyed = {table.partition_key, table.sort_key}
    keyed |= {key for index in table.indexes for key in (index.partition_key, index.sort_key)}
    return dynamodb.create_table(
        TableName=f"{table.name}-{secrets.token_hex(4)}",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": key.name, "AttributeType": key.type.value} for key in keyed if key is not None
        ],
        KeySchema=_schema(table.partition_key, table.sort_key),
        GlobalSecondaryIndexes=[
            {
                "IndexName": index.name,
                "KeySchema": _schema(index.partition_key, index.sort_key),
                "Projection": {"ProjectionType": "ALL"},
            }
            for index in table.indexes
        ],
    )


class Tables:
    def __init__(self, roster: Any, keys: Any) -> None:
        self.roster = roster
        self.keys = keys

    def stored(self) -> str:
        """Everything the two tables hold, as text."""
        return repr([self.roster.scan()["Items"], self.keys.scan()["Items"]])

    def key_items(self) -> list[dict[str, Any]]:
        return self.keys.scan()["Items"]


@pytest.fixture
def tables(monkeypatch: pytest.MonkeyPatch) -> Iterator[Tables]:
    """The roster, with two students, and the keys table, empty, in moto."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        tables = Tables(_create(dynamodb, ROSTER), _create(dynamodb, KEYS))
        for entry in (OTTER, HERON):
            tables.roster.put_item(Item={**entry.to_item(), **WRITTEN_BEFORE.get(entry.github_id, {})})
        yield tables


@pytest.fixture
def issuer(stub: Stub, tables: Tables, monkeypatch: pytest.MonkeyPatch) -> handler.Issuer:
    """The issuer its environment describes, calling the stub, on the stub's clock."""
    monkeypatch.setenv("RHI_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("RHI_COURSE_END", COURSE_END)
    monkeypatch.setenv("RHI_ROSTER_TABLE", tables.roster.name)
    monkeypatch.setenv("RHI_KEYS_TABLE", tables.keys.name)
    github = GitHub(token_url=stub.url + TOKEN_PATH, user_url=stub.url + USER_PATH)
    return handler.from_environment(github=github, clock=lambda: stub.now)


def event(method: str, path: str, body: str | None = None, *, encoded: bool = False) -> dict[str, Any]:
    """An event as the load balancer gives it to the function."""
    return {
        "requestContext": {"elb": {}},
        "httpMethod": method,
        "path": path,
        "queryStringParameters": {},
        "headers": {"content-type": "application/json", "host": "example.com"},
        "body": base64.b64encode((body or "").encode()).decode() if encoded else body or "",
        "isBase64Encoded": encoded,
    }


def login(issuer: handler.Issuer, device_code: object, installation: object = None, *, encoded: bool = False) -> Any:
    """One login, and the issuer's answer: its status and its JSON."""
    if installation is None:
        installation = secrets.token_hex(16)
    body = json.dumps({"device_code": device_code, "installation": installation})
    answer = issuer.answer(event("POST", "/auth/login", body, encoded=encoded))
    assert answer["isBase64Encoded"] is False
    assert answer["headers"]["Content-Type"] == "application/json"
    return answer["statusCode"], json.loads(answer["body"])


def refused(refusal: Refusal) -> tuple[int, dict[str, dict[str, str]]]:
    """The whole of a refusal's answer: its fixed status, code and sentence."""
    return refusal.status, {"error": {"code": refusal.value, "message": refusal.sentence}}


def test_a_roster_member_gets_a_key_and_only_its_hash_is_stored(
    stub: Stub, tables: Tables, issuer: handler.Issuer, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    installation = secrets.token_hex(16)
    status, answer = login(issuer, stub.device_code(OTTER.github_id), installation)

    assert status == 200
    assert set(answer) == {"key", "issued_at", "expires_at", "previous_issued_at"}
    key = answer["key"]
    assert is_valid_key(key)

    # The store holds one key item, under the SHA-256 of the key, and the key itself nowhere.
    (item,) = tables.key_items()
    stored = StudentKey.from_item(item)
    assert stored.key_hash == key_hash(key)
    assert (stored.github_id, stored.installation, stored.issued_at) == (OTTER.github_id, installation, NOW)
    assert key not in tables.stored()

    # GitHub gave one token, which went to the user address and nowhere else: no table, no log
    # (captured from the debug level, where the SDK repeats what it sends), no output, no answer.
    assert stub.calls == [TOKEN_PATH, USER_PATH]
    (token,) = stub.tokens
    output = capfd.readouterr()
    for secret in (token, key):
        assert secret not in caplog.text
        assert secret not in output.out + output.err
    assert "PutItem" in caplog.text
    assert token not in tables.stored()
    assert token not in json.dumps(answer)


def test_the_config_route_answers_the_client_id_and_nothing_else(stub: Stub, issuer: handler.Issuer) -> None:
    # Through the function's own entry point, which reads the same environment as the fixture.
    answer = handler.handler(event("GET", "/auth/config"), None)

    assert answer["statusCode"] == 200
    assert json.loads(answer["body"]) == {"client_id": CLIENT_ID}
    assert stub.calls == []

    # The two routes are the only ones, each under its own method.
    for method, path in (("POST", "/auth/config"), ("GET", "/auth/login"), ("GET", "/auth/"), ("GET", "/")):
        answer = handler.handler(event(method, path), None)
        assert answer["statusCode"] == 404
        assert json.loads(answer["body"])["error"]["code"] == "not_found"
    assert stub.calls == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(lambda code, installation: {"device_code": code[:-1], "installation": installation}, id="short"),
        pytest.param(lambda code, installation: {"device_code": code + "0", "installation": installation}, id="long"),
        pytest.param(
            lambda code, installation: {"device_code": code[:-1] + "/", "installation": installation}, id="alphabet"
        ),
        pytest.param(lambda code, installation: {"device_code": [code], "installation": installation}, id="not-text"),
        pytest.param(lambda code, installation: {"installation": installation}, id="no-code"),
        pytest.param(lambda code, installation: {"device_code": code}, id="no-installation"),
        pytest.param(lambda code, installation: {"device_code": code, "installation": "laptop"}, id="installation"),
        pytest.param(
            lambda code, installation: {"device_code": code, "installation": installation + "\n"}, id="line-break"
        ),
        pytest.param(lambda code, installation: [code, installation], id="not-an-object"),
        pytest.param(lambda code, installation: None, id="no-body"),
    ],
)
def test_malformed_input_calls_nobody(stub: Stub, tables: Tables, issuer: handler.Issuer, body: Any) -> None:
    # The device code is one GitHub would redeem: only the shape of the request refuses it.
    code, installation = stub.device_code(OTTER.github_id), secrets.token_hex(16)
    request = body(code, installation)
    text = "" if request is None else json.dumps(request)
    # The request as it is, as the load balancer encodes it, and cut short so that it is not JSON.
    events = [event("POST", "/auth/login", text), event("POST", "/auth/login", text, encoded=True)]
    events += [event("POST", "/auth/login", text[:-1]), {**events[1], "body": "!" + events[1]["body"]}]

    for sent in events:
        answer = issuer.answer(sent)
        assert (answer["statusCode"], json.loads(answer["body"])) == refused(Refusal.INVALID_REQUEST)

    assert stub.calls == []
    assert tables.key_items() == []
    # The same code in a request of the right shape is redeemed: nothing else was wrong with it.
    assert login(issuer, code, installation)[0] == 200


def test_outsiders_are_refused(stub: Stub, tables: Tables, issuer: handler.Issuer) -> None:
    # GitHub confirms both users; neither ID is in the roster.
    answers = [login(issuer, stub.device_code(outsider)) for outsider in (OTTER.github_id + 1, 99)]

    assert stub.calls == [TOKEN_PATH, USER_PATH] * 2
    assert answers[0] == answers[1] == refused(Refusal.NOT_ENROLLED)
    assert tables.key_items() == []
    # The sentence is a constant: it names no student and no number.
    for entry in (OTTER, HERON):
        assert entry.alias not in Refusal.NOT_ENROLLED.sentence
    assert not any(character.isdigit() for character in Refusal.NOT_ENROLLED.sentence)


@pytest.mark.parametrize(
    ("said", "code"),
    [
        ("authorization_pending", "authorization_pending"),
        ("slow_down", "slow_down"),
        ("expired_token", "expired_token"),
        ("access_denied", "login_refused"),
        ("incorrect_device_code", "login_refused"),
        ("the user address refuses", "github_unavailable"),
        ("no answer", "github_unavailable"),
    ],
)
def test_a_code_github_does_not_redeem_has_a_fixed_answer(
    stub: Stub, tables: Tables, issuer: handler.Issuer, said: str, code: str
) -> None:
    if said == "the user address refuses":
        device_code = stub.device_code(OTTER.github_id)
        stub.user_status = 401
    elif said == "no answer":
        device_code = stub.device_code(OTTER.github_id)
        issuer = dataclasses.replace(issuer, github=GitHub(token_url="http://127.0.0.1:1/", user_url=""))
    else:
        device_code = stub.device_code(said)

    # The answer is the refusal's constants, whole: nothing the stub said is in it.
    status, answer = login(issuer, device_code)
    assert (status, answer) == refused(Refusal(code))
    assert answer["error"]["code"] == code
    assert tables.key_items() == []


def test_the_previous_issue_time_is_shown(stub: Stub, tables: Tables, issuer: handler.Issuer) -> None:
    status, first = login(issuer, stub.device_code(OTTER.github_id))
    assert status == 200
    assert first["issued_at"] == NOW
    assert first["previous_issued_at"] is None

    # An hour later, from another installation, in a body the load balancer encoded.
    stub.now = NOW + 3600
    status, second = login(issuer, stub.device_code(OTTER.github_id), encoded=True)
    assert status == 200
    assert second["issued_at"] == NOW + 3600
    assert second["previous_issued_at"] == first["issued_at"]

    # Another student's first login is not told of the keys of the first.
    status, other = login(issuer, stub.device_code(HERON.github_id))
    assert status == 200
    assert other["previous_issued_at"] is None
    assert len({first["key"], second["key"], other["key"]}) == 3


@pytest.mark.parametrize("student", [OTTER, HERON], ids=lambda student: student.alias)
def test_the_key_carries_the_roster_limits(
    stub: Stub, tables: Tables, issuer: handler.Issuer, student: RosterEntry
) -> None:
    status, answer = login(issuer, stub.device_code(student.github_id))
    assert status == 200

    item = tables.keys.get_item(Key={"key_hash": key_hash(answer["key"])})["Item"]
    stored = StudentKey.from_item(item)
    assert stored.alias == student.alias
    assert stored.concurrency_limit == student.concurrency_limit
    # No budget, even from an entry the release before wrote with one.
    assert "daily_token_budget" not in item
    # The expiry is the course end the function was given, and the answer says the same.
    assert stored.expires_at == EXPIRES_AT == answer["expires_at"]
    assert stored.revoked_at is None
