"""`rhi login` against stubs of GitHub and of the issuer, with every key made at run time."""

from __future__ import annotations

import datetime
import os
import re
import secrets
import stat
import string
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import keyring.backend
import pytest
from keyring.backends import fail

from rhi_cli import login, main, store

ENDPOINT = "https://example.com"
INTERVAL = 7


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


def new_key() -> str:
    return "rhi_" + secrets.token_urlsafe(32)


class Stubs:
    """GitHub's device endpoint and the issuer's two routes; `logins` are the issuer's answers in turn."""

    def __init__(self, *logins: tuple[int, Any]) -> None:
        self.client_id = "Iv23" + secrets.token_hex(8)
        self.device_code = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(40))
        self.user_code = secrets.token_hex(2).upper() + "-" + secrets.token_hex(2).upper()
        self.logins = list(logins)
        self.calls: list[login.Call] = []

    def __call__(self, call: login.Call) -> tuple[int, Any]:
        self.calls.append(call)
        if (call.method, call.url) == ("GET", ENDPOINT + "/auth/config"):
            return 200, {"client_id": self.client_id}
        if (call.method, call.url) == ("POST", login.GITHUB_DEVICE_CODE_URL):
            assert call.fields == {"client_id": self.client_id}
            return 200, {
                "device_code": self.device_code,
                "user_code": self.user_code,
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": INTERVAL,
            }
        if (call.method, call.url) == ("POST", ENDPOINT + "/auth/login"):
            return self.logins.pop(0)
        return 404, {"error": {"code": "not_found", "message": "The issuer has no such route."}}

    def sent(self, url: str) -> list[login.Call]:
        return [call for call in self.calls if call.url == url]


def issued(key: str, previous: int | None = None) -> tuple[int, Any]:
    return 200, {"key": key, "issued_at": 1_900_000_000, "expires_at": 1_910_000_000, "previous_issued_at": previous}


def refused(code: str) -> tuple[int, Any]:
    return 400, {"error": {"code": code, "message": f"refused: {code}"}}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    before = keyring.get_keyring()
    keyring.set_keyring(MemoryKeychain())
    yield tmp_path / "rhi"
    keyring.set_keyring(before)


def run(stubs: Stubs, endpoint: str | None = ENDPOINT, clock: Clock | None = None) -> Clock:
    clock = clock or Clock()
    login.login(endpoint, transport=stubs, sleep=clock.sleep, clock=lambda: clock.now)
    return clock


@pytest.mark.parametrize("keychain", [True, False], ids=["keychain", "file"])
def test_a_login_stores_the_key_and_never_prints_it(keychain: bool, capfd: pytest.CaptureFixture[str]) -> None:
    if not keychain:
        keyring.set_keyring(fail.Keyring())
    key = new_key()
    stubs = Stubs(issued(key))
    run(stubs)
    assert store.load_key() == key
    if keychain:
        assert keyring.get_password(store.SERVICE, store.USERNAME) == key
    output = capfd.readouterr()
    assert key not in output.out + output.err
    assert stubs.user_code in output.out
    assert "https://github.com/login/device" in output.out


def test_pending_and_slowed_down_answers_are_polled_at_the_right_interval() -> None:
    stubs = Stubs(
        refused("authorization_pending"),
        refused("authorization_pending"),
        refused("slow_down"),
        refused("authorization_pending"),
        issued(new_key()),
    )
    clock = run(stubs)
    step = login.SLOW_DOWN_STEP
    assert clock.sleeps == [INTERVAL, INTERVAL, INTERVAL, INTERVAL + step, INTERVAL + step]
    assert [call.fields for call in stubs.sent(ENDPOINT + "/auth/login")][0] == {
        "device_code": stubs.device_code,
        "installation": store.load_config().installation,
    }


def test_another_refusal_stops_the_login() -> None:
    stubs = Stubs(refused("not_enrolled"))
    with pytest.raises(login.LoginError, match="not_enrolled"):
        run(stubs)
    assert store.load_key() is None


def test_the_client_id_is_the_one_the_issuer_served() -> None:
    stubs = Stubs(issued(new_key()))
    run(stubs)
    (device,) = stubs.sent(login.GITHUB_DEVICE_CODE_URL)
    assert device.fields == {"client_id": stubs.client_id}


def test_a_second_login_sends_the_same_installation_and_reuses_the_endpoint(home: Path) -> None:
    first, second = Stubs(issued(new_key())), Stubs(issued(new_key()))
    run(first)
    run(second, endpoint=None)
    (one,), (two,) = first.sent(ENDPOINT + "/auth/login"), second.sent(ENDPOINT + "/auth/login")
    assert one.fields and two.fields
    assert re.fullmatch(r"[0-9A-Za-z_-]{16,64}", one.fields["installation"])
    assert one.fields["installation"] == two.fields["installation"]
    assert store.load_config() == store.Config(ENDPOINT, one.fields["installation"])
    assert stat.S_IMODE((home / store.CONFIG_FILE).stat().st_mode) == 0o600


def test_without_an_endpoint_the_command_says_how_to_give_one(capsys: pytest.CaptureFixture[str]) -> None:
    stubs = Stubs()
    with pytest.raises(login.LoginError):
        run(stubs, endpoint=None)
    assert stubs.calls == []
    assert main.main(["login"]) == 1
    assert "rhi login --endpoint" in capsys.readouterr().err


def test_the_package_holds_no_endpoint_and_no_client_id() -> None:
    client = Path(main.__file__).resolve().parents[1]
    text = "\n".join(path.read_text(encoding="utf-8") for path in [*client.rglob("*.py"), *client.glob("*.toml")])
    assert set(re.findall(r"https?://([A-Za-z0-9.-]+)", text)) <= {"github.com"}
    assert not re.search(r"\bIv[0-9]+\.?[0-9A-Za-z]{8,}", text)


def test_the_fallback_file_is_private(home: Path) -> None:
    keyring.set_keyring(fail.Keyring())
    key = new_key()
    previous = os.umask(0o022)
    try:
        store.save_key(key)
    finally:
        os.umask(previous)
    path = home / store.KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load_key() == key

    path.chmod(0o644)
    with pytest.raises(store.StoreError):
        store.load_key()


def test_the_previous_issue_time_is_printed(capsys: pytest.CaptureFixture[str]) -> None:
    previous = 1_700_000_000 + secrets.randbelow(100_000_000)
    run(Stubs(issued(new_key(), previous)))
    expected = datetime.datetime.fromtimestamp(previous, tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert expected in capsys.readouterr().out
