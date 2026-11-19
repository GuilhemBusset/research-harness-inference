"""`rhi login`: GitHub's device flow, redeemed by the endpoint's key issuer (system design, section 6.4).

1. The endpoint's address comes from `--endpoint` once, from the course's private channel, and is
   stored (`rhi_cli.store`); later logins reuse it.
2. The login app's client ID comes from the issuer, `GET /auth/config`: neither value is in this
   package.
3. GitHub gives a device code for that client ID, and a short code the student confirms at the
   address GitHub names.
4. The CLI sends the device code, with this installation's identifier, to the issuer's
   `POST /auth/login`, at the interval GitHub gave, five seconds longer after each `slow_down`
   (RFC 8628, section 3.5), until the issuer answers with a key or a refusal, or the code expires.
5. The key goes to the store and is never printed. The command prints when the student's previous
   key was issued, so a login the student did not start shows.
6. Claude Code and Codex are pointed at the endpoint, each with `rhi token` as the helper that gives
   it the key (`rhi_cli.harness`). Codex's profile `rhi` names the default pool's model, which the
   CLI reads from the gateway's `GET /v1/models` with the new key. The key is written to neither file,
   and the command prints each file it changed: a second login changes none.

Every call goes through a `Transport`, and the waiting through `sleep` and `clock`: the tests give
stubs of all three.
"""

from __future__ import annotations

import argparse
import datetime
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO, Any, TextIO

from rhi_cli import harness, store

GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
CONFIG_PATH = "/auth/config"
LOGIN_PATH = "/auth/login"
MODELS_PATH = "/v1/models"

# RFC 8628, section 3.5: the interval when GitHub names none, and the step a `slow_down` adds.
DEFAULT_INTERVAL = 5
SLOW_DOWN_STEP = 5
TIMEOUT_SECONDS = 30

NO_ENDPOINT = (
    "No endpoint is stored yet. Run `rhi login --endpoint <address>` once, "
    "with the address from the course's private channel."
)


class LoginError(Exception):
    """A login that cannot go on; the message is for the student and never holds the key."""


@dataclass(frozen=True)
class Call:
    """One HTTP call. GitHub's device endpoint takes a form; the issuer takes JSON.

    `headers` are sent with it: the gateway is given the key in one.
    """

    method: str
    url: str
    fields: Mapping[str, str] | None = None
    form: bool = False
    headers: Mapping[str, str] | None = None


# The status of the answer and its body as JSON, or None when the body is not JSON.
type Transport = Callable[[Call], tuple[int, Any]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A device code or a key is never sent on to another address."""

    def redirect_request(
        self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> None:
        return None


def http_transport(call: Call) -> tuple[int, Any]:
    """The transport of a real login: the standard library's HTTP client."""
    data = None
    headers = {"Accept": "application/json", "User-Agent": "rhi-cli", **(call.headers or {})}
    if call.fields is not None:
        if call.form:
            data = urllib.parse.urlencode(call.fields).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(call.fields).encode()
            headers["Content-Type"] = "application/json"
    request = urllib.request.Request(call.url, data=data, headers=headers, method=call.method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, TimeoutError) as error:
        host = urllib.parse.urlsplit(call.url).hostname
        raise LoginError(f"{host} could not be reached: check the address and your connection.") from error
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def _endpoint(address: str) -> str:
    """The endpoint's address as stored: https, a host, no trailing slash."""
    parts = urllib.parse.urlsplit(address.strip())
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise LoginError("The endpoint must be an https:// address.")
    return f"https://{parts.netloc}{parts.path.rstrip('/')}"


def _member(body: Any, name: str, kind: type) -> Any:
    value = body.get(name) if isinstance(body, dict) else None
    return value if isinstance(value, kind) and not isinstance(value, bool) else None


def _when(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def login(
    endpoint: str | None,
    *,
    transport: Transport = http_transport,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
) -> None:
    """Log in, store the key, and print what the student needs; `LoginError` when it fails."""
    out = out or sys.stdout
    config = store.load_config()
    address = _endpoint(endpoint) if endpoint is not None else config.endpoint
    if not address:
        raise LoginError(NO_ENDPOINT)
    installation = config.installation or secrets.token_urlsafe(32)
    if (address, installation) != (config.endpoint, config.installation):
        store.save_config(store.Config(endpoint=address, installation=installation))

    status, body = transport(Call("GET", address + CONFIG_PATH))
    client_id = _member(body, "client_id", str)
    if status != 200 or not client_id:
        raise LoginError(f"The endpoint gave no login configuration (HTTP {status}): check its address.")

    status, device = transport(Call("POST", GITHUB_DEVICE_CODE_URL, {"client_id": client_id}, form=True))
    device_code = _member(device, "device_code", str)
    user_code = _member(device, "user_code", str)
    verification_uri = _member(device, "verification_uri", str)
    if status != 200 or not (device_code and user_code and verification_uri):
        raise LoginError(f"GitHub gave no device code (HTTP {status}): try again later.")
    interval = _member(device, "interval", int) or DEFAULT_INTERVAL
    expires_in = _member(device, "expires_in", int) or 900

    print(f"Open {verification_uri} and enter the code {user_code}", file=out)
    print("Confirm only a code that your own terminal has just printed.", file=out, flush=True)

    deadline = clock() + expires_in
    while True:
        sleep(interval)
        if clock() >= deadline:
            raise LoginError("The code has expired: run rhi login again.")
        status, answer = transport(
            Call("POST", address + LOGIN_PATH, {"device_code": device_code, "installation": installation})
        )
        key = _member(answer, "key", str)
        if status == 200 and key:
            break
        error = _member(answer, "error", dict) or {}
        code = _member(error, "code", str)
        if code == "authorization_pending":
            continue
        if code == "slow_down":
            interval += SLOW_DOWN_STEP
            continue
        message = _member(error, "message", str)
        raise LoginError(message or f"The endpoint refused the login (HTTP {status}).")

    where = store.save_key(key)
    print(f"Logged in. Your key is stored in {where}.", file=out)
    previous = _member(answer, "previous_issued_at", int)
    if previous is None:
        print("This is the first key issued to you.", file=out)
    else:
        print(
            f"Your previous key was issued at {_when(previous)}. If you did not log in then, tell the instructor.",
            file=out,
        )
    _configure_harnesses(address, key, transport, out)


def _default_model(address: str, key: str, transport: Transport) -> str:
    """The default pool's model name, read from the gateway with the key just issued."""
    status, models = transport(Call("GET", address + MODELS_PATH, headers={"Authorization": f"Bearer {key}"}))
    model = harness.default_model(models) if status == 200 else None
    if model is None:
        raise LoginError(f"The endpoint named no default model (HTTP {status}), so Codex is not configured.")
    return model


def _configure_harnesses(address: str, key: str, transport: Transport, out: TextIO) -> None:
    """Point each harness at the endpoint and print each file changed; one that fails does not stop the other."""
    steps = (
        lambda: harness.configure_claude_code(address),
        lambda: harness.configure_codex(address, _default_model(address, key, transport)),
    )
    failures = []
    for step in steps:
        try:
            changed = step()
        except (LoginError, harness.HarnessError) as error:
            failures.append(str(error))
            continue
        if changed is not None:
            print(f"Wrote {changed}", file=out)
    if failures:
        raise LoginError("Your key is stored, but the harnesses are not all configured. " + " ".join(failures))
    print(f"Claude Code now uses the endpoint. Codex uses it with `codex --profile {harness.NAME}`.", file=out)


def _run(arguments: argparse.Namespace) -> int:
    try:
        login(arguments.endpoint)
    except (LoginError, store.StoreError) as error:
        print(f"rhi login: {error}", file=sys.stderr)
        return 1
    return 0


def register(commands: argparse._SubParsersAction) -> None:
    """Add `rhi login` to the CLI's commands."""
    parser = commands.add_parser("login", help="log in with GitHub and store your key")
    parser.add_argument("--endpoint", help="the endpoint's address, given once; later logins reuse it")
    parser.set_defaults(run=_run)
