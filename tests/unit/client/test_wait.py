"""`rhi status`, `rhi wait` and `rhi revoke`, with every key made at run time.

The status is answered by a stand-in for the gateway (`Gateway`, below), which has the three
states and the two causes of the system design, section 5.3, and no other route. GitHub's device
endpoint and the issuer's routes are stubs. One clock, which the test moves, is the CLI's and the
stand-in's.
"""

from __future__ import annotations

import argparse
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

from rhi_cli import login, main, revoke, store, wait
from rhi_common.causes import Cause
from rhi_common.keys import generate_key

ENDPOINT = "https://example.com"
INTERVAL = 7
# When the stand-in's sessions end, in seconds since the epoch: 2027-01-15 13:30 UTC.
SESSION_ENDS_AT = 1_800_019_800


# The pools the stand-in lists, in its order.
POOLS = ("coursework", "mock", "evening")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Gateway:
    """The gateway behind the CLI's transport: `GET /status`, to the stored key alone, and no other route.

    `reads` gives, for a pool, what it reads from which second on, in order: `no_session`,
    `capacity_unavailable`, `warming` or `ready`. A pool has no session before its first second,
    and one that is not named never has one. A pool whose session is on gives when it ends.
    """

    reads: dict[str, dict[float, str]] = field(default_factory=dict)
    clock: Clock = field(default_factory=Clock)
    key: str = field(default_factory=generate_key)

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def record(self, name: str) -> dict[str, str | int]:
        """The pool's record now."""
        read = [what for since, what in self.reads.get(name, {}).items() if since <= self.clock.now]
        what = read[-1] if read else "no_session"
        if what == "no_session":
            return {"model": name, "state": "unavailable", "cause": Cause.NO_SESSION.value}
        session: dict[str, str | int] = {"model": name, "session_ends_at": SESSION_ENDS_AT}
        if what == "capacity_unavailable":
            return {**session, "state": "unavailable", "cause": Cause.CAPACITY_UNAVAILABLE.value}
        return {**session, "state": what}

    @staticmethod
    def refusal(status: int, cause: Cause) -> tuple[int, Any]:
        return status, {"type": "error", "error": {"type": cause.value, "message": cause.sentence}}

    def __call__(self, call: login.Call) -> tuple[int, Any]:
        """The CLI's transport: the status and the body the gateway answers."""
        assert call.url.startswith(ENDPOINT + "/")
        path = call.url.removeprefix(ENDPOINT)
        self.calls.append((call.method, path, self.clock.now))
        if (call.headers or {}).get("Authorization") != f"Bearer {self.key}":
            return self.refusal(401, Cause.INVALID_KEY)
        if (call.method, path) == ("GET", "/status"):
            return 200, {"pools": [self.record(name) for name in POOLS]}
        return self.refusal(404, Cause.NOT_FOUND)

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


@pytest.fixture
def two_hours_ahead_of_utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The time zone of the student's machine, for the test."""
    with monkeypatch.context() as patch:
        patch.setenv("TZ", "UTC-2")
        time.tzset()
        yield
    time.tzset()


@pytest.mark.usefixtures("two_hours_ahead_of_utc")
def test_status_prints_each_pool_with_its_state_its_cause_and_the_end_of_its_session(
    gateway: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway.reads.update(coursework={0: "ready"}, mock={0: "capacity_unavailable"})
    assert main.main(["status"]) == 0
    output = capsys.readouterr().out
    coursework, mock, evening = output.splitlines()
    assert coursework.split()[:2] == ["coursework", "ready"]
    assert mock.split()[:3] == ["mock", "unavailable", "capacity_unavailable"]
    # A pool whose session is on says when it ends, in the machine's own time; a pool with none says no time.
    assert "15:30" in coursework and "15:30" in mock
    assert evening.split() == ["evening", "unavailable", "no_session"]
    assert gateway.key not in output
    assert gateway.calls == [("GET", "/status", 0)]


def test_a_wait_reads_only_the_status_every_10_seconds_until_the_pool_is_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The session is on: an instance starts, is lost at second 35, and the pool is ready at second 125.
    gateway = Gateway({"mock": {0: "warming", 35: "capacity_unavailable", 125: "ready"}})
    logged_in(gateway.key)
    assert gateway.wait("mock") == 0
    assert gateway.calls == [("GET", "/status", at) for at in range(0, 131, wait.POLL_SECONDS)]
    # Each change is printed once, under the pool's name.
    printed = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert [line[1:] for line in printed] == [["warming"], ["unavailable", "capacity_unavailable"], ["ready"]]
    assert all(line[0].startswith("mock") for line in printed)


def test_the_10_seconds_run_from_one_status_call_to_the_next_whatever_an_answer_takes() -> None:
    gateway = Gateway({"mock": {0: "warming", 20: "ready"}})
    logged_in(gateway.key)

    def slow(call: login.Call) -> tuple[int, Any]:
        # Each answer takes 2 seconds, and the one asked for at second 10 takes 13.
        answer = gateway(call)
        gateway.clock.now += 13 if gateway.clock.now == 10 else 2
        return answer

    assert wait.wait("mock", transport=slow, sleep=gateway.clock.sleep, clock=lambda: gateway.clock.now) == 0
    # Second 10 follows second 0 by the interval; the next call follows the slow answer at once.
    assert [at for _, _, at in gateway.calls] == [0, 10, 23]


def test_a_wait_gives_up_at_its_timeout(capsys: pytest.CaptureFixture[str]) -> None:
    gateway = Gateway({"mock": {0: "capacity_unavailable"}})
    logged_in(gateway.key)
    assert gateway.wait("mock", 3) == 1
    assert gateway.clock.now == 3 * 60
    assert "not ready after 3 minutes" in capsys.readouterr().err

    # Without the option, the timeout is 20 minutes.
    parser = argparse.ArgumentParser()
    wait.register(parser.add_subparsers())
    assert parser.parse_args(["wait", "mock"]).timeout == 20
    assert parser.parse_args(["wait", "mock", "--timeout", "3"]).timeout == 3
    unhurried = Gateway({"mock": {0: "warming"}}, key=gateway.key)
    assert unhurried.wait("mock") == 1
    assert unhurried.clock.now == 20 * 60


def test_an_answer_that_arrives_after_the_timeout_is_not_a_success(capsys: pytest.CaptureFixture[str]) -> None:
    gateway = Gateway({"mock": {0: "warming", 55: "ready"}})
    logged_in(gateway.key)

    def slow(call: login.Call) -> tuple[int, Any]:
        # The status asked for at second 50 takes 15 seconds: it reads ready, past the timeout of one minute.
        if call.url.endswith("/status") and gateway.clock.now >= 50:
            gateway.clock.now += 15
        return gateway(call)

    result = wait.wait("mock", 1, transport=slow, sleep=gateway.clock.sleep, clock=lambda: gateway.clock.now)
    assert result == 1
    assert gateway.calls[-1] == ("GET", "/status", 65)
    assert "not ready after 1 minutes" in capsys.readouterr().err


def test_a_pool_with_no_session_stops_the_wait_with_its_cause(
    gateway: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main.main(["wait", "evening"]) == 1
    assert gateway.calls == [("GET", "/status", 0)]
    assert "no_session" in capsys.readouterr().err

    # A session that ends during the wait stops it too, at the first status that says so.
    ended = Gateway({"mock": {0: "warming", 25: "no_session"}}, key=gateway.key)
    assert ended.wait("mock") == 1
    assert [at for _, _, at in ended.calls] == [0, 10, 20, 30]
    assert "no_session" in capsys.readouterr().err


def test_an_error_of_the_gateway_or_a_name_the_status_does_not_list_stops_the_wait(
    gateway: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main.main(["wait", "no-such-pool"]) == 1
    assert gateway.calls == [("GET", "/status", 0)]
    assert "no-such-pool" in capsys.readouterr().err

    # A gateway that refuses the stored key: its cause is told, and the wait ends.
    another = Gateway({"mock": {0: "warming"}})
    assert another.wait("mock") == 1
    assert another.calls == [("GET", "/status", 0)]
    assert "invalid_key" in capsys.readouterr().err


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
