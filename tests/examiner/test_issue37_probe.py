"""Independent reporter checks; credentials are generated and AWS is always stubbed."""

import datetime
import http.server
import io
import json
import logging
import secrets
import threading
import tomllib
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import boto3
import jwt
import pytest
from botocore.stub import Stubber
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from rhi_reporter import github


class Reply(io.BytesIO):
    status = 201


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setenv("RHI_REPORTER_SECRET", "reporter-test")
    monkeypatch.setenv("RHI_REPORTER_REPOSITORY", "example-owner/example-repo")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    fields = {"app_id": "321", "installation_id": "654", "private_key": pem}
    aws = boto3.client(
        "secretsmanager", region_name="us-east-1", aws_access_key_id="stub", aws_secret_access_key="stub"
    )
    with Stubber(aws) as stubber:
        data: dict[str, Any] = {"now": 1800000000.0, "calls": [], "tokens": [], "jwts": [], "fields": fields}

        def open_request(request, timeout):
            assert request.get_method() == "POST"
            path = request.selector
            bearer = request.get_header("Authorization").removeprefix("Bearer ")
            data["calls"].append((path, request.data, bearer))
            if path == "/app/installations/654/access_tokens":
                claims = jwt.decode(
                    bearer, key.public_key(), algorithms=["RS256"], issuer="321",
                    options={"verify_exp": False, "verify_iat": False},
                )
                assert claims["iat"] <= data["now"] < claims["exp"] <= claims["iat"] + 600
                data["jwts"].append(bearer)
                token = secrets.token_urlsafe(32)
                data["tokens"].append(token)
                expiry = datetime.datetime.fromtimestamp(data["now"] + 3600, datetime.UTC).isoformat()
                return Reply(json.dumps({"token": token, "expires_at": expiry}).encode())
            assert path == "/repos/example-owner/example-repo/statuses/" + "a" * 40
            assert bearer == data["tokens"][-1]
            assert json.loads(request.data) == {
                "state": "success", "context": "verify/test", "description": "Verified"
            }
            return Reply(b"{}")

        monkeypatch.setattr(github._OPENER, "open", open_request)
        data["stubber"] = stubber
        data["client"] = github.StatusClient(aws, clock=lambda: data["now"])
        yield data
        stubber.assert_no_pending_responses()


def post(client):
    client.post_status("a" * 40, "success", "verify/test", "Verified")


def queue_secret(rig, fields):
    rig["stubber"].add_response(
        "get_secret_value", {"SecretString": json.dumps(fields)}, {"SecretId": "reporter-test"}
    )


def test_signing_cache_renewal_and_no_credential_logs(rig, caplog):
    caplog.set_level(logging.DEBUG)
    queue_secret(rig, rig["fields"])
    start = rig["now"]
    for elapsed in (0, 100, 3300):
        rig["now"] = start + elapsed
        post(rig["client"])
    assert len(rig["tokens"]) == 1
    rig["now"] = start + 3301
    post(rig["client"])
    assert len(rig["tokens"]) == 2
    paths = [path for path, _, _ in rig["calls"]]
    assert paths.count("/app/installations/654/access_tokens") == 2
    assert len(paths) == 6
    for value in [*rig["tokens"], *rig["jwts"], *rig["fields"]["private_key"].splitlines()[1:-1]]:
        assert value not in caplog.text


@pytest.mark.parametrize("missing", ["absent", "app_id", "installation_id", "private_key"])
def test_missing_credentials_are_one_safe_error(rig, missing, caplog):
    caplog.set_level(logging.DEBUG)
    marker = "aws-message-" + secrets.token_hex(10)
    if missing == "absent":
        rig["stubber"].add_client_error(
            "get_secret_value", "ResourceNotFoundException", marker,
            expected_params={"SecretId": "reporter-test"},
        )
    else:
        queue_secret(rig, {k: v for k, v in rig["fields"].items() if k != missing})
    with pytest.raises(github.CredentialError) as caught:
        post(rig["client"])
    assert str(caught.value) == str(github.CredentialError())
    assert marker not in "".join(traceback.format_exception(caught.value)) + caplog.text
    assert rig["calls"] == []


@pytest.mark.parametrize("phase", ["exchange", "status"])
def test_refusals_report_only_status_and_code(rig, monkeypatch, caplog, phase):
    queue_secret(rig, rig["fields"])
    if phase == "status":
        post(rig["client"])
    marker = "untrusted-response-" + secrets.token_hex(10)
    body = json.dumps({"message": marker, "errors": [{"code": "invalid", "message": marker}]}).encode()

    def refuse(request, timeout):
        headers: Any = {}  # a stand-in for the answer's headers, which nothing reads
        raise urllib.error.HTTPError(request.full_url, 422, marker, headers, io.BytesIO(body))

    monkeypatch.setattr(github._OPENER, "open", refuse)
    with pytest.raises(github.RefusedError) as caught:
        post(rig["client"])
    assert (caught.value.status, caught.value.code) == (422, "invalid")
    assert marker not in "".join(traceback.format_exception(caught.value)) + caplog.text


def test_interrupted_refusal_is_still_a_safe_refusal(rig, monkeypatch):
    queue_secret(rig, rig["fields"])
    post(rig["client"])
    marker = "untrusted-response-" + secrets.token_hex(10)

    class Refusal(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.send_response(403, marker)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"7\r\npartial\r\n")
            self.close_connection = True  # the refusal body ends before its terminating chunk

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refusal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    monkeypatch.setattr(github._OPENER, "open", opener.open)
    rig["client"]._api_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(github.RefusedError) as caught:
            post(rig["client"])
        assert caught.value.status == 403
        assert marker not in "".join(traceback.format_exception(caught.value))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_reporter_uses_existing_pins():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    group = project["dependency-groups"]["reporter"]
    assert {pin.split("==")[0] for pin in group} >= {"boto3", "PyJWT", "cryptography"}
    assert all("==" in pin for pin in group)
