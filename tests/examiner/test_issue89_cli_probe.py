"""Independent login, private storage and package checks for issue 89."""

from __future__ import annotations

import datetime
import io
import re
import secrets
import stat
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import keyring.backend
import keyring.errors
import pytest
from keyring.backends import fail

from rhi_cli import login, main, store


class ProbeKeychain(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}
        self.reject_writes = False

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.reject_writes:
            raise keyring.errors.PasswordSetError("Keychain write failed")
        self.entries[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        self.entries.pop((service, username), None)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ProbeKeychain]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    before = keyring.get_keyring()
    backend = ProbeKeychain()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(before)


def test_flow_and_repeated_installation(isolated: ProbeKeychain) -> None:
    address = "https://example.com"
    client_id = secrets.token_hex(10)
    device_code = secrets.token_hex(20)
    user_code = secrets.token_hex(4)
    key = secrets.token_urlsafe(32)
    previous = 1_700_000_000 + secrets.randbelow(1000)
    issued = {"key": key, "previous_issued_at": previous}
    replies: list[tuple[int, Any]] = [
        (400, {"error": {"code": "authorization_pending"}}),
        (400, {"error": {"code": "slow_down"}}),
        (400, {"error": {"code": "authorization_pending"}}),
        (200, issued),
        (200, issued),
    ]
    sent: list[dict[str, str]] = []
    sleeps: list[float] = []
    now = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    def transport(call: login.Call) -> tuple[int, Any]:
        if call.url == address + "/auth/config":
            assert call.method == "GET"
            return 200, {"client_id": client_id}
        if call.url == "https://github.com/login/device/code":
            assert call.method == "POST" and call.form
            assert call.fields == {"client_id": client_id}
            return 200, {
                "device_code": device_code,
                "user_code": user_code,
                "verification_uri": "https://github.com/login/device",
                "interval": 3,
                "expires_in": 900,
            }
        assert call.url == address + "/auth/login" and call.method == "POST"
        assert call.fields is not None
        sent.append(dict(call.fields))
        return replies.pop(0)

    output = io.StringIO()
    login.login(address, transport=transport, sleep=sleep, clock=lambda: now, out=output)
    assert sleeps == [3, 3, 8, 8]
    assert store.load_key() == key
    assert isolated.get_password(store.SERVICE, store.USERNAME) == key
    assert key not in output.getvalue()
    assert user_code in output.getvalue()
    assert "https://github.com/login/device" in output.getvalue()
    expected_time = datetime.datetime.fromtimestamp(previous, datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    assert expected_time in output.getvalue()
    installation = store.load_config().installation
    assert installation is not None and re.fullmatch(r"[0-9A-Za-z_-]{16,64}", installation)
    login.login(None, transport=transport, sleep=sleep, clock=lambda: now, out=output)
    assert all(fields == {"device_code": device_code, "installation": installation} for fields in sent)
    assert store.load_config().endpoint == address


def test_missing_endpoint_dispatch(isolated: ProbeKeychain, capsys: pytest.CaptureFixture[str]) -> None:
    assert main.main(["login"]) != 0
    assert "--endpoint" in capsys.readouterr().err
    package = Path(main.__file__).parent
    source = "\n".join(path.read_text() for path in package.glob("*.py"))
    assert set(re.findall(r"https?://([A-Za-z0-9.-]+)", source)) <= {"github.com"}


def test_private_fallback(isolated: ProbeKeychain) -> None:
    keyring.set_keyring(fail.Keyring())
    key = secrets.token_urlsafe(32)
    store.save_key(key)
    path = store.config_dir() / store.KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load_key() == key
    path.chmod(0o644)
    with pytest.raises(store.StoreError):
        store.load_key()


def test_latest_key_survives_keychain_write_failure(isolated: ProbeKeychain) -> None:
    store.save_key(secrets.token_urlsafe(32))
    replacement = secrets.token_urlsafe(32)
    isolated.reject_writes = True
    store.save_key(replacement)
    assert store.load_key() == replacement, "A successful fallback must not return the superseded keychain key"


def test_package_entry_and_pins() -> None:
    root = Path(__file__).resolve().parents[2]
    package = tomllib.loads((root / "client/pyproject.toml").read_text())
    locked = tomllib.loads((root / "uv.lock").read_text())
    versions = {entry["name"]: entry["version"] for entry in locked["package"]}
    assert package["project"]["scripts"]["rhi"] == "rhi_cli.main:main"
    for dependency in package["project"]["dependencies"]:
        name, version = dependency.split("==")
        assert versions[name] == version
