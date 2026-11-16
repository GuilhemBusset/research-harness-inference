"""Reading the GitHub Apps back (src/rhi/owner_apps.py), against a stand-in for GitHub.

The stand-in, on the loopback interface, answers as GitHub does for a private App: 404 to every
request for the App made without one of the App's own tokens, a JSON Web Token its key signed
(checked with `openssl` against the public key) or an installation token minted from one. The
keys are made with `openssl` in the test's temporary folder. tests/unit/test_owner_setup.py runs
the walkthrough against the same stand-in.
"""

from __future__ import annotations

import base64
import json
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from rhi import owner_apps

REPOSITORY = "example-owner/example-repo"
OTHER = "example-owner/another-repo"
OWNER_LOGIN = "gho_the-owners-own-login"  # what `gh auth token` would give: never an App's token
REPORTER = {"statuses": "write", "metadata": "read"}
FACTORY = {"contents": "write", "pull_requests": "write", "issues": "write", "statuses": "read"}
FACTORY |= {"checks": "read", "actions": "read", "metadata": "read"}


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class StandInApp:
    app_id: int
    slug: str
    permissions: dict[str, str]
    public_key: Path
    client_id: str
    device_flow: bool
    # Each installation: its ID, its repository selection, and the repositories it reaches.
    installations: list[tuple[int, str, list[str]]] = field(default_factory=list)

    def described(self) -> dict[str, Any]:
        """The App as GitHub's API describes it."""
        return {"id": self.app_id, "slug": self.slug, "client_id": self.client_id, "permissions": self.permissions}


class GitHub:
    """GitHub's API under /api, and its device code endpoint under /web."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir()
        self.apps: dict[int, StandInApp] = {}
        self.tokens: dict[str, int] = {}  # an installation token, and the installation it was minted for
        self.authorizations: list[str] = []  # the Authorization header of every request, or ""
        github = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # quiet: no log line per request
                pass

            def do_GET(self) -> None:
                self.answer()

            def do_POST(self) -> None:
                self.answer()

            def answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                authorization = self.headers.get("Authorization") or ""
                github.authorizations.append(authorization)
                status, data = github.route(self.command, self.path, authorization.removeprefix("Bearer "), body)
                encoded = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.env = {owner_apps.API_VAR: f"{base}/api", owner_apps.WEB_VAR: f"{base}/web"}

    def app(
        self,
        app_id: int,
        slug: str,
        installations: list[tuple[int, str, list[str]]] | None = None,
        permissions: dict[str, str] | None = None,
        *,
        device_flow: bool = True,
    ) -> Path:
        """Registers a private App, and returns its private key file."""
        key, public = self.folder / f"{slug}.pem", self.folder / f"{slug}.pub"
        if not key.exists():
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-out", key], check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", public], check=True, capture_output=True)
        client_id = f"Iv1.client-id-{slug}"
        self.apps[app_id] = StandInApp(app_id, slug, permissions or {}, public, client_id, device_flow, installations or [])
        return key

    def signed_by(self, token: str) -> StandInApp | None:
        """The App whose key signed this JWT, when it is one, current and of that App."""
        try:
            header, claims, signature = token.split(".")
            app = self.apps[int(json.loads(_unb64(claims))["iss"])]
            stated = json.loads(_unb64(claims))
        except (ValueError, KeyError, TypeError):
            return None
        if not stated["iat"] <= time.time() <= stated["exp"] or stated["exp"] - stated["iat"] > 600:
            return None
        signature_file = self.folder / f"signature-{secrets.token_hex(8)}"
        signature_file.write_bytes(_unb64(signature))
        verify = ["openssl", "dgst", "-sha256", "-verify", app.public_key, "-signature", signature_file]
        verified = subprocess.run(verify, input=f"{header}.{claims}".encode(), capture_output=True, check=False)
        return app if verified.returncode == 0 else None

    def installation(self, installation_id: int) -> tuple[StandInApp, tuple[int, str, list[str]]] | None:
        for app in self.apps.values():
            for one in app.installations:
                if one[0] == installation_id:
                    return app, one
        return None

    def route(self, method: str, path: str, token: str, body: str) -> tuple[int, Any]:
        path = urllib.parse.urlsplit(path).path
        signer = self.signed_by(token) if token.count(".") == 2 else None
        held = self.installation(self.tokens[token]) if token in self.tokens else None
        if method == "POST" and path == "/web/login/device/code":
            client_id = urllib.parse.parse_qs(body).get("client_id", [""])[0]
            if any(app.client_id == client_id and app.device_flow for app in self.apps.values()):
                return 200, {"device_code": secrets.token_hex(8), "user_code": "WXYZ-1234", "interval": 5}
            return 400, {"error": "device_flow_disabled"}
        if signer and method == "GET" and path == "/api/app":
            return 200, signer.described()
        if signer and method == "GET" and path == "/api/app/installations":
            return 200, [{"id": one[0], "repository_selection": one[1]} for one in signer.installations]
        minted = path.removeprefix("/api/app/installations/").removesuffix("/access_tokens")
        if signer and method == "POST" and minted.isdigit() and int(minted) in [i for i, *_ in signer.installations]:
            token = f"ghs_{secrets.token_hex(16)}"
            self.tokens[token] = int(minted)
            return 201, {"token": token, "expires_at": "2099-01-01T00:00:00Z"}
        if held and method == "GET" and path == "/api/installation/repositories":
            names = held[1][2]
            return 200, {"total_count": len(names), "repositories": [{"full_name": name} for name in names]}
        if held and method == "GET" and path.startswith("/api/apps/"):
            for app in self.apps.values():
                if path == f"/api/apps/{app.slug}":
                    return 200, app.described()
        return 404, {"message": "Not Found"}

    def tokens_seen(self) -> set[str]:
        """Every bearer token a request carried."""
        return {header.removeprefix("Bearer ") for header in self.authorizations if header}


@pytest.fixture
def github(tmp_path: Path) -> Any:
    stand_in = GitHub(tmp_path / "github")
    yield stand_in
    stand_in.server.shutdown()


def read_back(github: GitHub, capsys: pytest.CaptureFixture[str], *arguments: object) -> tuple[int, str, str]:
    status = owner_apps.main([str(argument) for argument in arguments], github.env)
    printed = capsys.readouterr()
    return status, printed.out, printed.err


def ask(github: GitHub, path: str, token: str = "") -> int:
    """The HTTP status the stand-in gives a request with this token, or none."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(f"{github.env[owner_apps.API_VAR]}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def secrets_in(github: GitHub, *keys: Path) -> list[str]:
    """What no output may hold: each key's lines, every token, the client IDs."""
    held = [line for key in keys for line in key.read_text(encoding="utf-8").splitlines() if "KEY" not in line]
    return held + sorted(github.tokens_seen() | set(github.tokens)) + [app.client_id for app in github.apps.values()]


def test_an_app_is_read_back_with_its_own_key(github: GitHub, capsys: pytest.CaptureFixture[str]) -> None:
    key = github.app(202, "course-reporter", [(404, "selected", [REPOSITORY])], REPORTER)
    other_key = github.app(303, "course-factory", [(505, "selected", [REPOSITORY])], FACTORY)

    # As GitHub does for a private App: nothing for the owner's login, or for no token at all.
    for token in ("", OWNER_LOGIN):
        assert [ask(github, path, token) for path in ("/app", "/app/installations", "/apps/course-reporter")] == [404] * 3

    status, out, err = read_back(github, capsys, "reporter", "--app-id", 202, "--key-file", key, "--repository", REPOSITORY)
    assert status == 0, err
    assert "The App course-reporter, ID 202" in out
    assert "metadata=read statuses=write" in out
    assert "These are the permissions of section 8.6." in out
    assert f"Installation 404: selected repositories: {REPOSITORY}" in out
    assert "installed on this repository alone" in out
    assert not [value for value in secrets_in(github, key) if value in out + err]

    # Another App's key signs a JWT GitHub refuses: nothing is read back.
    status, out, err = read_back(github, capsys, "reporter", "--app-id", 202, "--key-file", other_key, "--repository", REPOSITORY)
    assert (status, out) == (1, "")
    assert "refused: GitHub answered HTTP 404" in err


@pytest.mark.parametrize(
    "installations",
    [
        [(404, "all", [REPOSITORY, OTHER])],  # every repository of the account
        [(404, "selected", [REPOSITORY, OTHER])],
        [(404, "selected", [OTHER])],
        [(404, "selected", [REPOSITORY]), (405, "selected", ["someone-else/a-repo"])],  # another account
    ],
)
def test_an_app_beyond_this_repository_alone_is_refused(
    github: GitHub, capsys: pytest.CaptureFixture[str], installations: list[tuple[int, str, list[str]]]
) -> None:
    key = github.app(303, "course-factory", installations, FACTORY)

    status, out, err = read_back(github, capsys, "factory", "--app-id", 303, "--key-file", key, "--repository", REPOSITORY)
    assert status == 1
    assert "These are the permissions of section 8.6." in out
    assert "Installation 404: " in out  # what GitHub reports is shown, then refused
    assert err.startswith("refused: ")
    assert "this repository alone" not in out


def test_the_login_app_is_read_with_the_reporters_token(github: GitHub, capsys: pytest.CaptureFixture[str]) -> None:
    key = github.app(202, "course-reporter", [(404, "selected", [REPOSITORY])], REPORTER)
    github.app(101, "course-login")
    login = ["login", "--slug", "course-login", "--app-id", 202, "--key-file", key]

    status, out, err = read_back(github, capsys, *login)
    assert status == 0, err
    assert "The App course-login, ID 101" in out
    assert "Its permissions: none" in out
    assert "These are the permissions of section 8.6." in out
    assert "device flow is on" in out
    assert not [value for value in secrets_in(github, key) if value in out + err]

    # The device flow off: refused, and the client ID still never shown.
    github.app(101, "course-login", device_flow=False)
    status, out, err = read_back(github, capsys, *login)
    assert status == 1
    assert "device flow is off" in err
    assert not [value for value in secrets_in(github, key) if value in out + err]
