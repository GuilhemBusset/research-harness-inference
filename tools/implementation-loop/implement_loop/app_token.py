"""The factory's own GitHub identity: an installation token of its GitHub App (ADR 0001,
amendment 2), minted from the App ID, the installation ID and a private key file that three
environment variables name. Standard library and the `openssl` program only.

With the three variables set, the engine's `gh` calls and its git pushes and fetches run as the
App. With none of them set, nothing here acts and the engine keeps the login `gh` already has.
One or two of them set is an error, never a silent fall back to the owner's login.

Where the token is, and where it is not:

- in this process, in memory (`token`): a token with less than ten minutes left is replaced;
- in the environment of each `gh` process the engine starts (`gh_env`), and nowhere in the
  engine's own environment, which every other process it starts inherits;
- in the answer this file gives git when git runs it as a credential helper for github.com
  (`git_env` configures that, `credential` answers). git starts a helper for each push and each
  fetch; the helper is a new process that holds no token, so it mints one each time, which is
  why a run of any length keeps pushing.

Neither the token nor the key is written to a file, an event or a log, and no message of this
module carries either, nor what GitHub or openssl answered: only fixed text and an HTTP status.
Python never reads the key: `openssl` signs with it.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

VARS = ("LOOP_GITHUB_APP_ID", "LOOP_GITHUB_APP_INSTALLATION_ID", "LOOP_GITHUB_APP_KEY_FILE")
# Where GitHub's API is. The engine's tests point this at a stub on the loopback interface.
API_VAR = "LOOP_GITHUB_API_URL"
API = "https://api.github.com"
API_VERSION = "2022-11-28"
HOST = "github.com"                 # the one host the credential helper answers for
MARGIN_S = 600                      # a token handed out has at least this long left
JWT_LIFE_S = 540                    # GitHub refuses a JWT that lasts more than ten minutes
CLOCK_DRIFT_S = 60                  # the JWT is dated a minute back, as GitHub recommends
TIMEOUT_S = 30
GIT_KEY = f"credential.https://{HOST}.helper"

# What ADR 0001 withholds from the factory, and what the engine cannot land without.
FORBIDDEN_PERMISSIONS = ("workflows", "administration")
READS_CHECKS = ("read", "write")


class AppTokenError(RuntimeError):
    """No token could be had. `transient` says the request may succeed if repeated: GitHub was
    not reached, or answered 429 or 5xx. The message is fixed text and never carries a secret."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class App:
    app_id: str
    installation_id: str
    key_file: str
    api: str = API


@dataclass(frozen=True)
class Held:
    token: str
    expires: float   # seconds since the epoch
    app: App


def configured(env: Mapping[str, str] | None = None) -> App | None:
    """The App the environment names, or None when it names none. Raises when only some of the
    three variables are set, or when one of them cannot be what it says."""
    env = os.environ if env is None else env
    values = {name: (env.get(name) or "").strip() for name in VARS}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise AppTokenError(f"{', '.join(missing)} not set: the factory's GitHub App needs {', '.join(VARS)}, all "
                            "three, or none of them to keep the login `gh` has")
    app_id, installation_id, key_file = (values[name] for name in VARS)
    for name, value in zip(VARS[:2], (app_id, installation_id), strict=True):
        if not (value.isascii() and value.isdigit()):
            raise AppTokenError(f"{name} is not a number")
    if not Path(key_file).is_absolute():
        raise AppTokenError(f"{VARS[2]} is not an absolute path")
    return App(app_id, installation_id, key_file, _api(env.get(API_VAR) or API))


def _api(url: str) -> str:
    """The API's base URL. The App's signed request goes there: over TLS, or to this host."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")):
        raise AppTokenError(f"{API_VAR} is neither an https URL nor one on this host")
    return url.rstrip("/")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(app: App, now: float | None = None) -> str:
    """The App's signed statement of who it is (RS256), which GitHub exchanges for a token.
    `openssl` reads the key file and signs; this process never holds the key."""
    at = int(_now() if now is None else now)
    parts = ({"alg": "RS256", "typ": "JWT"},
             {"iat": at - CLOCK_DRIFT_S, "exp": at + JWT_LIFE_S, "iss": int(app.app_id)})
    signing = ".".join(_b64(json.dumps(part, separators=(",", ":")).encode()) for part in parts)
    if not Path(app.key_file).is_file():
        raise AppTokenError(f"the file {VARS[2]} names does not exist")
    try:
        proc = subprocess.run(["openssl", "dgst", "-sha256", "-sign", app.key_file], input=signing.encode(),
                              capture_output=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise AppTokenError("the `openssl` program could not be run to sign as the GitHub App") from None
    if proc.returncode != 0 or not proc.stdout:
        raise AppTokenError(f"openssl could not sign with the file {VARS[2]} names: is it the App's private key?")
    return f"{signing}.{_b64(proc.stdout)}"


_HINTS = {401: " (the App ID, the key or this host's clock is wrong)",
          403: " (GitHub refused the App)",
          404: " (GitHub knows no such App or installation)"}


def _call(app: App, method: str, path: str, what: str) -> dict[str, Any]:
    """One request signed as the App. An error names the HTTP status, never GitHub's own text."""
    request = urllib.request.Request(f"{app.api}/{path}", method=method, headers={
        "Authorization": f"Bearer {jwt(app)}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION, "User-Agent": "implement-loop"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:   # `_api` vetted the URL
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        code = e.code
        raise AppTokenError(f"GitHub answered HTTP {code} when asked for {what}{_HINTS.get(code, '')}",
                            transient=code == 429 or code >= 500) from None
    except (OSError, ValueError):   # no connection, a timeout, or an answer that is not JSON
        raise AppTokenError(f"GitHub could not be reached for {what}", transient=True) from None
    if not isinstance(data, dict):
        raise AppTokenError(f"GitHub's answer for {what} is not the object expected", transient=True)
    return data


def mint(app: App) -> Held:
    """A new installation token, with the moment GitHub says it expires (an hour from now)."""
    what = "an installation token"
    data = _call(app, "POST", f"app/installations/{app.installation_id}/access_tokens", what)
    token, expires_at = data.get("token"), data.get("expires_at")
    try:
        expires = datetime.strptime(str(expires_at), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        expires = None
    if not isinstance(token, str) or not token or expires is None:
        raise AppTokenError(f"GitHub's answer for {what} carries no token or no expiry", transient=True)
    return Held(token, expires, app)


def installation(app: App) -> dict[str, Any]:
    """What GitHub says of the installation: the App's `app_slug`, its `permissions`, its
    `repository_selection`. Read as the App, for the doctor."""
    return _call(app, "GET", f"app/installations/{app.installation_id}", "the App's installation")


def permission_problems(permissions: object) -> list[str]:
    """Why an installation with these permissions may not be the factory's identity: it holds
    one ADR 0001 withholds (GitHub must be the one that refuses a factory change to a workflow
    file or a ruleset), or it cannot read the check runs the engine lands on."""
    held = permissions if isinstance(permissions, dict) else {}
    problems = [f"it holds the `{name}` permission ({held[name]}), which the factory must not have"
                for name in FORBIDDEN_PERMISSIONS if held.get(name)]
    if held.get("checks") not in READS_CHECKS:
        problems.append("it cannot read checks, so the engine could never see `required` green")
    return problems


def _now() -> float:
    return time.time()


_lock = threading.Lock()
_held: list[Held] = []   # at most one: the token this process holds


def token(env: Mapping[str, str] | None = None) -> str | None:
    """A token of the App the environment names with at least ten minutes left: the one this
    process holds, or a new one when it holds none or one that is closer to its end. None when
    no App is named."""
    app = configured(env)
    if app is None:
        return None
    with _lock:
        if not _held or _held[0].app != app or _held[0].expires - _now() < MARGIN_S:
            _held[:] = [mint(app)]
        return _held[0].token


def gh_env(env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The environment of one `gh` process: the engine's, plus the token (`GH_TOKEN`, which
    `gh` prefers to any stored login). None when no App is named: `gh` then inherits the
    engine's environment and uses the login it has. Built for each call, so that the token is
    never in the engine's own environment."""
    env = os.environ if env is None else env
    held = token(env)
    return None if held is None else {**env, "GH_TOKEN": held}


def git_env(env: Mapping[str, str], python: str | None = None) -> dict[str, str]:
    """What the engine adds to its own environment so that every git process it starts, and
    every one `ship.sh` and the supervisor start, asks this file for github.com's credential: an
    empty helper first, which drops the helpers git's files configure (the owner's login), then
    this one. It joins the configuration git already takes from the environment
    (`sandbox.engine_git_env`) and names a command, never a token. Nothing when no App is
    named, or when the helper is already there (a run the supervisor launched)."""
    try:
        if configured(env) is None:
            return {}
    except AppTokenError:   # the doctor reports it, and no run starts
        return {}
    helper = f"!{shlex.quote(python or sys.executable)} -I {shlex.quote(str(Path(__file__).resolve()))} credential"
    try:
        count = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        count = 0
    if any(env.get(f"GIT_CONFIG_KEY_{i}") == GIT_KEY and env.get(f"GIT_CONFIG_VALUE_{i}") == helper for i in range(count)):
        return {}
    out = {"GIT_CONFIG_COUNT": str(count + 2)}
    for i, value in enumerate(("", helper), start=count):
        out[f"GIT_CONFIG_KEY_{i}"] = GIT_KEY
        out[f"GIT_CONFIG_VALUE_{i}"] = value
    return out


def credential(operation: str, asked: TextIO, answer: TextIO, problems: TextIO) -> int:
    """git's credential helper protocol. `get` for https://github.com is answered with a token;
    `store` and `erase` have nothing to do, since nothing is kept. When no token can be had,
    git is told to stop there instead of asking a person for a password."""
    if operation != "get":
        return 0
    wanted = dict(line.split("=", 1) for line in asked.read().splitlines() if "=" in line)
    if wanted.get("protocol") != "https" or wanted.get("host") != HOST:
        return 0
    try:
        held = token()
    except AppTokenError as e:
        print(f"implement: {e}", file=problems)
        answer.write("quit=true\n")
        return 1
    if held is not None:
        answer.write(f"username=x-access-token\npassword={held}\n")
    return 0


if __name__ == "__main__":   # `git` runs: <python> -I app_token.py credential get|store|erase
    if len(sys.argv) != 3 or sys.argv[1] != "credential":
        sys.exit("usage: app_token.py credential get|store|erase   (a git credential helper)")
    sys.exit(credential(sys.argv[2], sys.stdin, sys.stdout, sys.stderr))
