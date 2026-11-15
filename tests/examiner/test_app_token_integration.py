"""Independent issue #9 probes: local API, generated key, real git and sandbox."""

import base64
import io
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path.cwd() / "tools/implementation-loop"))
from implement_loop import app_token, cli, sandbox


@pytest.fixture
def identity(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    key = home / "key.pem"
    public = home / "public.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], capture_output=True, check=True)
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)],
                   capture_output=True, check=True)
    state = SimpleNamespace(tokens=[], now=time.time(), permissions={"checks": "read"}, signatures=0)

    class API(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self):
            encoded = self.headers["Authorization"].removeprefix("Bearer ")
            head, claims, signature = encoded.split(".")
            decode = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
            assert json.loads(decode(head))["alg"] == "RS256"
            claims = json.loads(decode(claims))
            assert claims["iss"] == 4242 and 0 < claims["exp"] - claims["iat"] <= 600
            sig = tmp_path / ("signature-" + secrets.token_hex(4))
            sig.write_bytes(decode(signature))
            checked = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public),
                                      "-signature", str(sig)], input=encoded.rsplit(".", 1)[0].encode(),
                                     capture_output=True, check=False)
            assert checked.returncode == 0
            state.signatures += 1
            if self.command == "POST":
                assert self.path == "/app/installations/777/access_tokens"
                state.tokens.append(secrets.token_hex(24))
                body = {"token": state.tokens[-1], "expires_at": datetime.fromtimestamp(
                    state.now + 3600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
            else:
                assert self.path == "/app/installations/777"
                body = {"app_slug": "examiner-factory", "repository_selection": "selected",
                        "permissions": state.permissions}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_POST = reply
        do_GET = reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), API)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    for name in list(os.environ):
        if name in (*app_token.VARS, *sandbox.GITHUB_VARS) or name.startswith("GIT_CONFIG_"):
            monkeypatch.delenv(name)
    for name, value in zip(app_token.VARS, ("4242", "777", str(key)), strict=True):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(app_token.API_VAR, f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setattr(app_token, "_held", [])
    monkeypatch.setattr(app_token, "_now", lambda: state.now)
    state.key, state.home = key, home
    yield state
    server.shutdown()
    server.server_close()
    worker.join()


def test_mint_and_signature(identity):
    token = app_token.token()
    assert token == identity.tokens[-1]
    assert identity.signatures == 1
    assert app_token._held[0].expires - identity.now >= 3598
    assert app_token.token() == token and len(identity.tokens) == 1


def test_every_gh_entry_point_renews(identity, monkeypatch):
    seen = []

    def run(args, **kwargs):
        assert args[0] == "gh"
        seen.append(kwargs["env"]["GH_TOKEN"])
        return subprocess.CompletedProcess(args, 0, "{}", "")

    client = cli.github("o/r")
    client._run = run
    client.get_issue(1)
    first = identity.tokens[-1]
    identity.now += 49 * 60
    client.get_issue(2)
    assert seen == [first, first]
    identity.now += 2 * 60
    client.get_issue(3)
    second = identity.tokens[-1]
    assert second != first and seen[-1] == second
    monkeypatch.setattr(client, "_failed_runs", lambda sha: [{"id": 1}])
    client.failed_log("a" * 40)
    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.repo_slug()
    cli._out(["gh", "auth", "status"])
    assert seen == [first, first, second, second, second, second]
    assert "GH_TOKEN" not in os.environ


def test_real_git_helper_mints_after_renewal(identity, monkeypatch, tmp_path):
    first = app_token.token()
    identity.now += 51 * 60
    assert app_token.token() != first
    for name, value in app_token.git_env(os.environ).items():
        monkeypatch.setenv(name, value)
    for name, value in sandbox.engine_git_env(os.environ, tmp_path / "no-hooks").items():
        monkeypatch.setenv(name, value)
    filled = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                            capture_output=True, text=True, check=True)
    assert "username=x-access-token" in filled.stdout
    assert "password=" + identity.tokens[-1] in filled.stdout
    assert identity.tokens[-1] != first and len(identity.tokens) == 3
    answer = io.StringIO()
    assert app_token.credential("get", io.StringIO("protocol=https\nhost=example.com\n\n"),
                                answer, io.StringIO()) == 0
    assert not answer.getvalue()
    assert "GH_TOKEN" not in os.environ
