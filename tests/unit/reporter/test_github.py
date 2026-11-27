"""The reporter's GitHub client, against a stub of GitHub's API and a stub of Secrets Manager.

Both stubs answer on one loopback port: the client's own HTTP calls and a real client of the AWS
SDK reach them, so the SDK's debug log is among the captured logs. The App's key is generated
here, and the stub of GitHub checks the App token's signature with its public half.
"""

from __future__ import annotations

import ast
import datetime
import http.server
import json
import logging
import secrets
import sys
import threading
import time
import tomllib
import traceback
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import boto3
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from rhi_reporter import github

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO = Path(__file__).resolve().parents[3]
APP_ID = "424242"
INSTALLATION_ID = "98765"
REPOSITORY = "octo-org/hello.world"
COMMIT = "c0ffee" * 6 + "abcd"
EXCHANGE = f"/app/installations/{INSTALLATION_ID}/access_tokens"
STATUS = f"/repos/{REPOSITORY}/statuses/{COMMIT}"
LIST = f"/repos/{REPOSITORY}/commits/{COMMIT}/statuses"


class Posted(TypedDict):
    state: str
    context: str
    description: str


POSTED: Posted = {"state": "success", "context": "deploy/prod", "description": "Deployed"}
# What a stub says in its own words, which no error and no log may repeat.
MARKER = "said-by-the-stub " + secrets.token_hex(8)


class Stub:
    """What the two services hold, and what they were asked. `now` is the clock the client shares."""

    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        ).decode()
        self.public_key = key.public_key()
        self.key_lines = pem.splitlines()[1:-1]
        self.secret: dict[str, str] | None = dict(app_id=APP_ID, installation_id=INSTALLATION_ID, private_key=pem)
        self.secret_reads = 0
        self.now = float(int(time.time()))
        self.refusals: dict[str, tuple[int, Any]] = {}
        # Answers given once each, in turn, before the path is answered as usual: status, JSON, headers.
        self.queued: dict[str, list[tuple[int, Any, dict[str, str]]]] = {}
        self.paths: list[str] = []
        self.calls: list[tuple[str, str]] = []
        # When each status was asked for, on the shared clock, and each wait the client made.
        self.attempts: list[float] = []
        self.sleeps: list[float] = []
        # The statuses the commit holds, newest first, and how many a page lists.
        self.listed: list[dict[str, Any]] = []
        self.page_size = 100
        self.next_page: str | None = None
        self.app_tokens: list[str] = []
        self.tokens: list[str] = []
        self.statuses: list[tuple[str, Any]] = []
        self.url = ""

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def answer(
        self, method: str, target_path: str, target: str | None, bearer: str, body: Any
    ) -> tuple[int, Any, dict[str, str]]:
        if target == "secretsmanager.GetSecretValue":
            self.secret_reads += 1
            if self.secret is None:
                return 400, {"__type": "ResourceNotFoundException", "Message": MARKER}, {}
            return 200, {"Name": body["SecretId"], "SecretString": json.dumps(self.secret)}, {}
        path, _, query = target_path.partition("?")
        self.paths.append(path)
        self.calls.append((method, path))
        if path == STATUS:
            self.attempts.append(self.now)
        if self.queued.get(path):
            return self.queued[path].pop(0)
        if path in self.refusals:
            return (*self.refusals[path], {})
        if method == "GET":
            return self.page(path, query, bearer)
        return (*self.created(path, bearer, body), {})

    def page(self, path: str, query: str, bearer: str) -> tuple[int, Any, dict[str, str]]:
        if path != LIST or not self.tokens or bearer != self.tokens[-1]:
            return 404, {"message": "Not Found"}, {}
        number = int(urllib.parse.parse_qs(query).get("page", ["1"])[0])
        listed = self.listed[(number - 1) * self.page_size : number * self.page_size]
        more = number * self.page_size < len(self.listed)
        following = self.next_page or f"{self.url}{LIST}?per_page=100&page={number + 1}"
        return 200, listed, {"Link": f'<{following}>; rel="next"'} if more else {}

    def created(self, path: str, bearer: str, body: Any) -> tuple[int, Any]:
        if path == EXCHANGE:
            try:
                claims = jwt.decode(
                    bearer,
                    self.public_key,
                    algorithms=["RS256"],
                    issuer=APP_ID,
                    options=dict(verify_exp=False, verify_iat=False),  # checked below, on the stub's clock
                )
            except jwt.PyJWTError:
                return 401, {"message": "the App token is not signed by the App's key"}
            if not claims["iat"] <= self.now < claims["exp"] <= claims["iat"] + 600:
                return 401, {"message": "the App token is not valid now, or for more than ten minutes"}
            self.app_tokens.append(bearer)
            self.tokens.append(secrets.token_urlsafe(30))
            expiry = datetime.datetime.fromtimestamp(self.now + 3600, datetime.UTC)
            return 201, {"token": self.tokens[-1], "expires_at": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if path == STATUS and self.tokens and bearer == self.tokens[-1]:
            self.statuses.append((bearer, body))
            return 201, {"state": body["state"]}
        return 404, {"message": "Not Found"}


class Handler(http.server.BaseHTTPRequestHandler):
    stub: Stub

    def do_POST(self) -> None:
        self.answer("POST")

    def do_GET(self) -> None:
        self.answer("GET")

    def answer(self, method: str) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        status, answer, headers = self.stub.answer(
            method, self.path, self.headers.get("X-Amz-Target"), bearer, json.loads(body or b"null")
        )
        if not status:
            return  # the connection closes with no answer
        data = json.dumps(answer).encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # the server's own access log
        pass


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def stub(key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture) -> Iterator[Stub]:
    caplog.set_level(logging.DEBUG)
    stub = Stub(key)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("Handler", (Handler,), {"stub": stub}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stub.url = f"http://127.0.0.1:{server.server_port}"
    yield stub
    server.shutdown()
    server.server_close()
    # Whatever the test did, the client asked GitHub for a token or to create a status, and
    # otherwise only read the commit's statuses.
    assert {path for method, path in stub.calls if method != "GET"} <= {EXCHANGE, STATUS}
    assert {path for method, path in stub.calls if method == "GET"} <= {LIST}


@pytest.fixture
def client(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> github.StatusClient:
    monkeypatch.setenv("RHI_REPORTER_SECRET", "reporter-app")
    monkeypatch.setenv("RHI_REPORTER_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("no_proxy", "127.0.0.1")  # the SDK reaches the stub directly, wherever the tests run
    aws = boto3.client(
        "secretsmanager",
        endpoint_url=stub.url,
        region_name="us-east-1",
        aws_access_key_id="stub",
        aws_secret_access_key="stub",
    )
    return github.StatusClient(aws, api_url=stub.url, clock=lambda: stub.now, sleep=stub.sleep)


def shown(error: BaseException) -> str:
    """Everything a log of the error would show: its text, its causes and their tracebacks."""
    return "".join(traceback.format_exception(error))


def test_a_status_is_posted_with_a_token_minted_from_the_key(stub: Stub, client: github.StatusClient) -> None:
    client.post_status(COMMIT, **POSTED)

    # The stub gives a token only for an App token that the public half of the key verifies.
    assert len(stub.app_tokens) == 1
    assert stub.paths == [EXCHANGE, STATUS]
    assert stub.statuses == [(stub.tokens[0], POSTED)]


def test_the_token_is_reused_until_five_minutes_before_its_expiry(
    stub: Stub, client: github.StatusClient, caplog: pytest.LogCaptureFixture
) -> None:
    issued = stub.now
    for elapsed in (0, 1800, 3600 - 300):
        stub.now = issued + elapsed
        client.post_status(COMMIT, **POSTED)
    assert len(stub.tokens) == 1

    stub.now = issued + 3600 - 299
    client.post_status(COMMIT, **POSTED)
    assert [token for token, _ in stub.statuses] == [*stub.tokens[:1] * 3, stub.tokens[1]]

    # The secret was read by the first status only. The logs are captured from the debug level,
    # where the SDK repeats what Secrets Manager answered: they hold neither the key nor a token.
    assert stub.secret_reads == 1
    for secret in (*stub.key_lines, *stub.app_tokens, *stub.tokens):
        assert secret not in caplog.text


@pytest.mark.parametrize("missing", ["the secret", "app_id", "installation_id", "private_key", "a key that signs"])
def test_a_missing_credential_is_one_error(
    stub: Stub, client: github.StatusClient, caplog: pytest.LogCaptureFixture, missing: str
) -> None:
    whole = dict(stub.secret or {})
    if missing == "the secret":
        stub.secret = None
    elif missing == "a key that signs":
        stub.secret = whole | {"private_key": MARKER}
    else:
        stub.secret = {name: value for name, value in whole.items() if name != missing}

    with pytest.raises(github.CredentialError) as caught:
        client.post_status(COMMIT, **POSTED)

    assert str(caught.value) == str(github.CredentialError())
    assert MARKER not in shown(caught.value)
    assert "botocore" not in shown(caught.value)
    assert MARKER not in caplog.text
    assert stub.app_tokens == []

    # The failure is not kept: once the owner has stored the secret, the next status is posted.
    stub.secret = whole
    client.post_status(COMMIT, **POSTED)
    assert len(stub.statuses) == 1


@pytest.mark.parametrize(
    ("path", "status", "answer", "code"),
    [
        (EXCHANGE, 401, {"message": MARKER}, None),
        (
            STATUS,
            422,
            {"message": MARKER, "errors": [{"resource": "Status", "code": "custom", "message": MARKER}]},
            "custom",
        ),
        (STATUS, 403, {"message": MARKER, "errors": [{"code": MARKER}]}, None),
        (STATUS, None, None, None),
    ],
    ids=["token", "status", "unknown-code", "no-answer"],
)
def test_a_refused_call_is_reported_by_its_status_and_code_only(
    stub: Stub,
    client: github.StatusClient,
    caplog: pytest.LogCaptureFixture,
    path: str,
    status: int | None,
    answer: Any,
    code: str | None,
) -> None:
    stub.refusals[path] = (status or 0, answer)

    with pytest.raises(github.RefusedError) as caught:
        client.post_status(COMMIT, **POSTED)

    assert (caught.value.status, caught.value.code) == (status, code)
    assert MARKER not in shown(caught.value)
    assert MARKER not in caplog.text
    for token in (*stub.app_tokens, *stub.tokens):
        assert token not in shown(caught.value)


def test_a_status_refused_with_a_retry_after_is_posted_again_once_it_has_passed(
    stub: Stub, client: github.StatusClient
) -> None:
    stub.queued[STATUS] = [(429, {"message": MARKER}, {"Retry-After": "2"})]
    client.post_status(COMMIT, **POSTED, deadline=stub.now + 30)
    assert len(stub.attempts) == 2
    assert stub.attempts[1] - stub.attempts[0] == 2
    assert len(stub.statuses) == 1


def test_a_wait_that_would_end_after_the_deadline_is_raised_not_waited(stub: Stub, client: github.StatusClient) -> None:
    stub.queued[STATUS] = [(429, {"message": MARKER}, {"Retry-After": "2"})]
    with pytest.raises(github.RefusedError) as caught:
        client.post_status(COMMIT, **POSTED, deadline=stub.now + 1)
    assert caught.value.status == 429
    assert (len(stub.attempts), stub.sleeps, stub.statuses) == (1, [], [])


@pytest.mark.parametrize(
    ("answers", "waits"),
    [
        ([(429, {}, {})], [1]),
        ([(403, {}, {"Retry-After": "3"})], [3]),
        ([(403, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "+5"})], [5]),
        ([(0, None, {})], [1]),
        ([(502, {}, {})] * 5, [1, 2, 4, 8, 8]),
    ],
    ids=["429", "403-retry-after", "403-rate-limit-reset", "no-answer", "5xx-doubled"],
)
def test_a_temporary_refusal_is_waited_out_until_the_deadline(
    stub: Stub, client: github.StatusClient, answers: list[tuple[int, Any, dict[str, str]]], waits: list[float]
) -> None:
    reset = {"X-RateLimit-Reset": str(int(stub.now) + 5)}
    stub.queued[STATUS] = [
        (status, answer, headers | reset if headers.get("X-RateLimit-Reset") else headers)
        for status, answer, headers in answers
    ]
    client.post_status(COMMIT, **POSTED, deadline=stub.now + 60)
    assert stub.sleeps == waits
    assert len(stub.statuses) == 1


def test_a_rate_limit_reset_that_has_come_is_posted_again_at_once(stub: Stub, client: github.StatusClient) -> None:
    come = (403, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(stub.now))})
    stub.queued[STATUS] = [come]
    client.post_status(COMMIT, **POSTED, deadline=stub.now + 0.5)
    assert (len(stub.attempts), stub.sleeps, len(stub.statuses)) == (2, [], 1)

    # Not twice in a row: GitHub is not asked again and again with no wait.
    stub.attempts.clear()
    stub.queued[STATUS] = [come, come]
    client.post_status(COMMIT, **POSTED, deadline=stub.now + 60)
    assert (len(stub.attempts), stub.sleeps) == (3, [2])


@pytest.mark.parametrize("lost", [(0, None), (502, {"message": MARKER})], ids=["no-answer", "5xx"])
@pytest.mark.parametrize("saved", [True, False], ids=["saved", "not-saved"])
def test_a_status_github_took_without_saying_so_is_not_posted_twice(
    stub: Stub, client: github.StatusClient, monkeypatch: pytest.MonkeyPatch, lost: tuple[int, Any], saved: bool
) -> None:
    stub.listed = [{"id": 7, **POSTED}]  # the same status, posted before: older than `newer_than`
    created = stub.created
    answers = [lost]

    def create(path: str, bearer: str, body: Any) -> tuple[int, Any]:
        code, answer = created(path, bearer, body)
        if path != STATUS:
            return code, answer
        if saved or not answers:
            stub.listed.insert(0, {"id": stub.listed[0]["id"] + 1, **body})
        return answers.pop(0) if answers else (code, answer)

    monkeypatch.setattr(stub, "created", create)
    client.post_status(COMMIT, **POSTED, deadline=stub.now + 60, newer_than=7)
    assert len(stub.attempts) == (1 if saved else 2)
    assert [one["id"] for one in stub.listed] == [8, 7]


@pytest.mark.parametrize(
    ("answer", "deadline"),
    [
        ((403, {"message": MARKER}, {}), 60),
        ((422, {"errors": [{"code": "custom"}]}, {"Retry-After": "1"}), 60),
        ((429, {}, {"Retry-After": "1"}), None),
    ],
    ids=["403-without-a-rate-limit", "422", "no-deadline"],
)
def test_any_other_refusal_is_raised_without_waiting(
    stub: Stub, client: github.StatusClient, answer: tuple[int, Any, dict[str, str]], deadline: int | None
) -> None:
    stub.queued[STATUS] = [answer]
    with pytest.raises(github.RefusedError) as caught:
        client.post_status(COMMIT, **POSTED, deadline=None if deadline is None else stub.now + deadline)
    assert caught.value.status == answer[0]
    assert (len(stub.attempts), stub.sleeps, stub.statuses) == (1, [], [])


def test_the_statuses_of_a_commit_are_read_from_every_page_whole(stub: Stub, client: github.StatusClient) -> None:
    # As GitHub lists them, each with its creator's record: a page holds far more than one answer.
    creator = {"login": "rhi-reporter[bot]", "type": "Bot", "url": "https://example.com/" + "x" * 900}
    stub.listed = [
        {"id": number, "context": f"measure/S5/run/{number}", "state": "success", "description": None if number == 1 else f"n={number}",
         "creator": creator, "created_at": "2026-11-27T12:00:00Z"}
        for number in range(150, 0, -1)
    ]  # fmt: skip
    assert len(json.dumps(stub.listed[:100])) > github.ANSWER_BYTES

    found = client.statuses(COMMIT)
    assert [one["id"] for one in found] == list(range(150, 0, -1))
    assert found[-1] == {"id": 1, "context": "measure/S5/run/1", "state": "success", "description": ""}
    assert [method for method, path in stub.calls if path == LIST] == ["GET", "GET"]


def test_a_next_page_elsewhere_than_github_s_api_is_not_asked_for(stub: Stub, client: github.StatusClient) -> None:
    stub.listed = [{"id": number, "context": "c", "state": "success", "description": ""} for number in range(3)]
    stub.page_size, stub.next_page = 2, "https://example.com/elsewhere"
    with pytest.raises(github.RefusedError):
        client.statuses(COMMIT)
    assert [path for _, path in stub.calls] == [EXCHANGE, LIST]


@pytest.mark.parametrize(
    ("repository", "commit"),
    [("no-owner", COMMIT), ("octo-org/hello/../../other", COMMIT), (REPOSITORY, "main")],
)
def test_only_a_repository_and_a_full_sha_make_the_address(
    stub: Stub, client: github.StatusClient, monkeypatch: pytest.MonkeyPatch, repository: str, commit: str
) -> None:
    monkeypatch.setenv("RHI_REPORTER_REPOSITORY", repository)

    with pytest.raises(ValueError, match="owner/name|full SHA"):
        client.post_status(commit, **POSTED)

    assert stub.paths == []
    assert stub.secret_reads == 0


def test_a_commit_status_is_the_only_write() -> None:
    # The stub fixture checks, after every test above, that no other address of GitHub was asked.
    public = [name for name in vars(github.StatusClient) if not name.startswith("_")]
    assert public == ["post_status", "statuses"]


def test_the_client_adds_no_dependency() -> None:
    tree = ast.parse(Path(github.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module or "" if isinstance(node, ast.ImportFrom) else alias.name).partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    # jwt is PyJWT, whose RSA signature is cryptography's; botocore comes with boto3.
    assert imported - sys.stdlib_module_names == {"boto3", "botocore", "jwt"}
    group = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["dependency-groups"]["reporter"]
    assert {pin.partition("==")[0] for pin in group} == {"boto3", "PyJWT", "cryptography"}
