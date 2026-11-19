"""`rhi status`, `rhi wait` and `rhi revoke`, with every key made at run time.

The status and the wake are the gateway's own local routes, in process: the application
(`rhi_gateway.app.build_app`) over the real readiness, waker and ledger, whose AWS clients are
doubles. GitHub's device endpoint and the issuer's routes are stubs. One clock, which the test
moves, is the CLI's and the gateway's.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import secrets
import string
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
import keyring.backend
import keyring.errors
import pytest
from keyring.backends import fail
from starlette.types import Message

from rhi_cli import login, main, revoke, store, wait
from rhi_common.keys import generate_key, key_hash
from rhi_common.pool_config import Pool, parse_pools
from rhi_common.records import Mode, PoolState, ScalerNote
from rhi_common.students import StudentKey
from rhi_gateway.app import build_app
from rhi_gateway.auth import Authenticator
from rhi_gateway.budget import InMemoryLedger
from rhi_gateway.keystore import InMemoryKeyStore
from rhi_gateway.local import LocalRoutes
from rhi_gateway.readiness import Readiness
from rhi_gateway.waker import Waker

ENDPOINT = "https://example.com"
NOW = 1_800_000_000
INTERVAL = 7


def pool(name: str, wake: str = "on-demand") -> Pool:
    entry = {
        "engine": "mock",
        "instance_types": ["t4g.small"],
        "prices": {"t4g.small": 0.0168},
        "max_instances": 1,
        "warm_instances": 0,
        "wake": wake,
        "target_concurrency": 64,
        "context_length": 8192,
        "largest_output": 1024,
    }
    return parse_pools({name: entry})[0]


POOLS = (pool("coursework"), pool("mock"), pool("evening", wake="scheduled-only"))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Gateway:
    """The gateway's local routes behind the CLI's transport, and the AWS clients its readiness and waker read.

    A pool has a healthy target from the second `ready_at` gives it, and is disabled by the admin
    from the second `disabled_at` gives it. The scaler's note for a pool says capacity was asked
    for once a wake for it has reached the gateway.
    """

    clock: Clock = field(default_factory=Clock)
    ready_at: dict[str, float] = field(default_factory=dict)
    disabled_at: dict[str, float] = field(default_factory=dict)
    budget: int = 2_000_000
    key: str = field(default_factory=generate_key)

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []
        self.woken: set[str] = set()
        self.student = StudentKey(
            key_hash=key_hash(self.key),
            github_id=4242,
            alias="otter",
            installation=None,
            issued_at=NOW - 3600,
            expires_at=NOW + 86400,
            revoked_at=None,
            daily_token_budget=self.budget,
            concurrency_limit=4,
        )
        self.ledger = InMemoryLedger(clock=self.time)
        self.readiness = Readiness({p.name: p.name for p in POOLS}, elbv2=self, table=self, clock=self.time)
        local = LocalRoutes(
            Authenticator(InMemoryKeyStore(self.student), wall_clock=self.time),
            POOLS,
            self.readiness.views,
            default_pool=POOLS[0],
            clock=self.time,
            waker=Waker(self.readiness, lambda_client=self, function_name="scaler", clock=self.time),
            ledger=self.ledger,
        )
        self.app = build_app(local=local)

    def time(self) -> float:
        return NOW + self.clock.now

    # The three AWS calls the gateway makes.
    def describe_target_health(self, *, TargetGroupArn: str) -> dict[str, Any]:
        healthy = self.clock.now >= self.ready_at.get(TargetGroupArn, float("inf"))
        return {"TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy"}}] if healthy else []}

    def get_item(self, *, Key: dict[str, str]) -> dict[str, Any]:
        name = Key["pool"]
        if Key["record"] == "mode":
            disabled = self.clock.now >= self.disabled_at.get(name, float("inf"))
            record = PoolState(pool=name, mode=Mode.DISABLED, set_by="admin", set_at=NOW)
            return {"Item": record.to_item()} if disabled else {}
        if name in self.woken:
            return {"Item": ScalerNote(pool=name, capacity_asked=1, no_capacity_at=None, written_at=NOW).to_item()}
        return {}

    def invoke(self, **arguments: Any) -> dict[str, Any]:
        return {"StatusCode": 202}

    def __call__(self, call: login.Call) -> tuple[int, Any]:
        """The CLI's transport: one readiness cycle, then the call through the application."""
        assert call.url.startswith(ENDPOINT + "/")
        path = call.url.removeprefix(ENDPOINT)
        self.calls.append((call.method, path, self.clock.now))
        self.readiness.refresh()
        if path == "/wake" and call.fields:
            self.woken.add(call.fields["model"])
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "http",
            "server": ("gateway.example.com", 80),
            "client": ("192.0.2.1", 50000),
            "method": call.method,
            "root_path": "",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(name.lower().encode(), value.encode()) for name, value in (call.headers or {}).items()],
            "state": {},
        }
        body = b"" if call.fields is None else json.dumps(call.fields).encode()
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": body, "more_body": False}

        async def keep(message: Message) -> None:
            sent.append(message)

        # The gateway's request log goes to its own standard output, not to the CLI's.
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(self.app(scope, receive, keep))
        return sent[0]["status"], json.loads(b"".join(message.get("body", b"") for message in sent[1:]))

    def wakes(self) -> list[float]:
        """When each wake reached the gateway, in seconds on the test's clock."""
        return [at for method, path, at in self.calls if (method, path) == ("POST", "/wake")]

    def wait(self, model: str, *arguments: float) -> int:
        """`rhi wait` on the test's clock; its exit status."""
        return wait.wait(model, *arguments, transport=self, sleep=self.clock.sleep, clock=lambda: self.clock.now)


class Stubs:
    """GitHub's device endpoint and the issuer's routes; `revocations` are the answers to `/auth/revoke-all` in turn."""

    def __init__(self, *revocations: tuple[int, Any]) -> None:
        self.client_id = "Iv23" + secrets.token_hex(8)
        self.device_code = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40))
        self.user_code = secrets.token_hex(2).upper() + "-" + secrets.token_hex(2).upper()
        self.revocations = list(revocations)
        self.calls: list[login.Call] = []

    def __call__(self, call: login.Call) -> tuple[int, Any]:
        self.calls.append(call)
        if (call.method, call.url) == ("GET", ENDPOINT + "/auth/config"):
            return 200, {"client_id": self.client_id}
        if (call.method, call.url) == ("POST", login.GITHUB_DEVICE_CODE_URL):
            return 200, {
                "device_code": self.device_code,
                "user_code": self.user_code,
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": INTERVAL,
            }
        if (call.method, call.url) == ("POST", ENDPOINT + "/auth/revoke"):
            return 200, {"revoked": 1}
        if (call.method, call.url) == ("POST", ENDPOINT + "/auth/revoke-all"):
            return self.revocations.pop(0)
        return 404, {"error": {"code": "not_found", "message": "The issuer has no such route."}}


PENDING = 400, {"error": {"code": "authorization_pending", "message": "The code is not confirmed yet."}}


class MemoryKeychain(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.entries.pop((service, username), None)


def unexpected_sleep(seconds: float) -> None:
    raise AssertionError(f"the command slept {seconds} seconds of real time")


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty store: no configuration, no key file, an empty keychain. No command sleeps for real."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(time, "sleep", unexpected_sleep)
    before = keyring.get_keyring()
    keyring.set_keyring(MemoryKeychain())
    yield tmp_path / "rhi"
    keyring.set_keyring(before)


def logged_in(key: str) -> None:
    store.save_config(store.Config(endpoint=ENDPOINT, installation=secrets.token_urlsafe(32)))
    store.save_key(key)


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Gateway:
    """A gateway the commands reach when `main` runs them, and a store logged in to it."""
    made = Gateway()
    logged_in(made.key)
    monkeypatch.setattr(login, "http_transport", made)
    return made


def test_status_prints_each_pool_with_its_state_and_cause(gateway: Gateway, capsys: pytest.CaptureFixture[str]) -> None:
    gateway.ready_at["coursework"] = 0
    assert main.main(["status"]) == 0
    output = capsys.readouterr().out
    assert [line.split() for line in output.splitlines()] == [
        ["coursework", "ready"],
        ["mock", "cold"],
        ["evening", "unavailable", "pool_scheduled_only"],
    ]
    assert gateway.key not in output
    assert gateway.wakes() == []


def test_a_wait_wakes_at_once_and_every_minute_until_the_pool_is_ready(capsys: pytest.CaptureFixture[str]) -> None:
    gateway = Gateway(ready_at={"mock": 3 * 60 + 1})
    logged_in(gateway.key)
    assert gateway.wait("mock") == 0
    assert gateway.wakes() == [0, 60, 120, 180]
    assert 181 <= gateway.clock.now < 181 + wait.POLL_SECONDS
    assert capsys.readouterr().out.splitlines() == ["mock: cold", "mock: warming", "mock: ready"]
    assert {path for _, path, _ in gateway.calls} == {"/wake", "/status"}


def test_a_wait_gives_up_at_its_timeout(capsys: pytest.CaptureFixture[str]) -> None:
    gateway = Gateway()
    logged_in(gateway.key)
    assert gateway.wait("mock", 3) != 0
    assert gateway.clock.now == 3 * 60
    assert gateway.wakes() == [0, 60, 120]
    assert "not ready after 3 minutes" in capsys.readouterr().err

    # Without the option, the timeout is 20 minutes.
    parser = argparse.ArgumentParser()
    wait.register(parser.add_subparsers())
    assert parser.parse_args(["wait", "mock"]).timeout == 20
    assert parser.parse_args(["wait", "mock", "--timeout", "3"]).timeout == 3
    unhurried = Gateway(key=gateway.key)
    assert unhurried.wait("mock") != 0
    assert unhurried.clock.now == 20 * 60


def test_an_answer_that_arrives_after_the_timeout_is_not_a_success(capsys: pytest.CaptureFixture[str]) -> None:
    gateway = Gateway(ready_at={"mock": 55})
    logged_in(gateway.key)

    def slow(call: login.Call) -> tuple[int, Any]:
        # The status asked for at second 50 takes 15 seconds: it reads ready, past the timeout of one minute.
        if call.url.endswith("/status") and gateway.clock.now >= 50:
            gateway.clock.now += 15
        return gateway(call)

    result = wait.wait("mock", 1, transport=slow, sleep=gateway.clock.sleep, clock=lambda: gateway.clock.now)
    assert result != 0
    assert gateway.calls[-1] == ("GET", "/status", 65)
    assert "not ready after 1 minutes" in capsys.readouterr().err


def test_an_unavailable_pool_stops_the_wait(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main.main(["wait", "evening"]) != 0
    assert gateway.calls == [("POST", "/wake", 0)]
    assert "pool_scheduled_only" in capsys.readouterr().err

    # A pool that turns unavailable during the wait stops it too, at the first status that says so.
    disabled = Gateway(disabled_at={"mock": 1}, key=gateway.key)
    assert disabled.wait("mock") != 0
    assert [path for _, path, _ in disabled.calls] == ["/wake", "/status"]
    assert "pool_disabled" in capsys.readouterr().err

    spent = Gateway(budget=1000, key=gateway.key)
    reservation = spent.ledger.reserve(
        spent.student, "earlier", b"{}", output_limit=0, hold_seconds=420, longest_request_seconds=1800
    )
    spent.ledger.settle(reservation, input_tokens=1000, output_tokens=0, ended_cleanly=True)
    monkeypatch.setattr(login, "http_transport", spent)
    assert main.main(["wait", "mock"]) != 0
    assert spent.calls == [("POST", "/wake", 0)]
    assert "budget_reached" in capsys.readouterr().err


def test_a_wait_for_a_claude_name_follows_the_default_pool(capsys: pytest.CaptureFixture[str]) -> None:
    gateway = Gateway(ready_at={"coursework": 15})
    logged_in(gateway.key)
    assert gateway.wait("claude-sonnet") == 0
    assert capsys.readouterr().out.splitlines() == ["coursework: cold", "coursework: ready"]


def test_revoke_one_or_all(home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for keychain in (True, False):
        keyring.set_keyring(MemoryKeychain() if keychain else fail.Keyring())
        key, stubs = generate_key(), Stubs()
        logged_in(key)
        monkeypatch.setattr(login, "http_transport", stubs)
        assert main.main(["revoke"]) == 0
        (sent,) = stubs.calls
        assert (sent.method, sent.url, sent.fields) == ("POST", ENDPOINT + "/auth/revoke", None)
        assert sent.headers == {"Authorization": f"Bearer {key}"}
        assert store.load_key() is None
        assert not (home / store.KEY_FILE).exists()
        assert key not in "".join(capsys.readouterr())

    # With nothing stored at all, every key is revoked through a fresh device flow.
    (home / store.CONFIG_FILE).unlink()
    stubs, clock = Stubs(PENDING, (200, {"revoked": 2})), Clock()
    monkeypatch.setattr(login, "http_transport", stubs)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    assert main.main(["revoke", "--all", "--endpoint", ENDPOINT]) == 0
    assert [(call.method, call.url) for call in stubs.calls] == [
        ("GET", ENDPOINT + "/auth/config"),
        ("POST", login.GITHUB_DEVICE_CODE_URL),
        ("POST", ENDPOINT + "/auth/revoke-all"),
        ("POST", ENDPOINT + "/auth/revoke-all"),
    ]
    assert stubs.calls[1].fields == {"client_id": stubs.client_id}
    assert stubs.calls[-1].fields == {"device_code": stubs.device_code}
    assert all(call.headers is None for call in stubs.calls)
    assert clock.sleeps == [INTERVAL, INTERVAL]
    assert stubs.user_code in capsys.readouterr().out
    assert store.load_key() is None


def test_revoking_every_key_removes_the_stored_one_and_a_refusal_keeps_it(capsys: pytest.CaptureFixture[str]) -> None:
    key, clock = generate_key(), Clock()
    logged_in(key)
    refused = Stubs((403, {"error": {"code": "not_enrolled", "message": "This account is not on the roster."}}))
    with pytest.raises(login.LoginError, match="roster"):
        revoke.revoke_all(transport=refused, sleep=clock.sleep, clock=lambda: clock.now)
    assert store.load_key() == key

    revoke.revoke_all(transport=Stubs((200, {"revoked": 1})), sleep=clock.sleep, clock=lambda: clock.now)
    assert store.load_key() is None
    assert key not in "".join(capsys.readouterr())


def test_a_keychain_that_locks_after_the_revocation_is_reported_not_taken_for_a_removal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class LockableKeychain(MemoryKeychain):
        locked = False

        def get_password(self, service: str, username: str) -> str | None:
            if self.locked:
                raise keyring.errors.KeyringError("locked")
            return super().get_password(service, username)

        def delete_password(self, service: str, username: str) -> None:
            if self.locked:
                raise keyring.errors.KeyringError("locked")
            super().delete_password(service, username)

    key, keychain, clock = generate_key(), LockableKeychain(), Clock()
    keyring.set_keyring(keychain)
    logged_in(key)

    def locking(stubs: Stubs) -> login.Transport:
        def transport(call: login.Call) -> tuple[int, Any]:
            keychain.locked = True
            return stubs(call)

        return transport

    monkeypatch.setattr(login, "http_transport", locking(Stubs()))
    assert main.main(["revoke"]) == 1
    told = capsys.readouterr()
    assert told.out == ""
    assert "revoked" in told.err and "could not be removed" in told.err

    with pytest.raises(store.StoreError, match="could not be removed"):
        revoke.revoke_all(ENDPOINT, transport=locking(Stubs((200, {"revoked": 1}))), sleep=clock.sleep)
    keychain.locked = False
    assert store.load_key() == key
    assert key not in "".join(capsys.readouterr())


def test_without_a_key_or_an_endpoint_the_commands_say_what_to_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stubs = Stubs()
    monkeypatch.setattr(login, "http_transport", stubs)
    for command in (["status"], ["wait", "mock"], ["revoke"]):
        assert main.main(command) == 1
        assert "rhi login" in capsys.readouterr().err
    assert main.main(["revoke", "--all"]) == 1
    assert "--endpoint" in capsys.readouterr().err
    assert stubs.calls == []
