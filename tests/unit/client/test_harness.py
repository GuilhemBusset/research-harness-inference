"""What `rhi login` writes for Claude Code and Codex, in scratch home folders, and `rhi token`.

The endpoint is a stub: GitHub's device endpoint, the issuer's routes and the gateway's
`GET /v1/models`. Every key and model name is made at run time.
"""

from __future__ import annotations

import io
import json
import secrets
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import keyring
import keyring.backend
import pytest

from rhi_cli import login, main, store

ENDPOINT = "https://example.com"

CLAUDE_CODE_BEFORE = {"model": "opus", "permissions": {"allow": ["Read"]}, "env": {"EDITOR": "vi"}}
# Codex's own configuration, which a login never writes, and what a student added to the profile.
CODEX_CONFIG = """\
model = "gpt-5"

[model_providers.other]
name = "other"
base_url = "https://example.com/other/v1"
"""
CODEX_BEFORE = """\
# kept: the student's own settings for the profile
model_reasoning_effort = "low"

[tui]
notifications = false
"""


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


class Endpoint:
    """The stub. Each login is issued a new key, and the models are listed only for the latest key."""

    def __init__(self) -> None:
        self.default = "model-" + secrets.token_hex(4)
        self.other = "model-" + secrets.token_hex(4)
        self.keys: list[str] = []

    def __call__(self, call: login.Call) -> tuple[int, Any]:
        route = (call.method, call.url)
        if route == ("GET", ENDPOINT + "/auth/config"):
            return 200, {"client_id": "client-" + secrets.token_hex(8)}
        if route == ("POST", login.GITHUB_DEVICE_CODE_URL):
            return 200, {
                "device_code": secrets.token_hex(20),
                "user_code": secrets.token_hex(4).upper(),
                "verification_uri": "https://github.com/login/device",
            }
        if route == ("POST", ENDPOINT + "/auth/login"):
            self.keys.append(new_key())
            return 200, {"key": self.keys[-1], "previous_issued_at": None}
        if route == ("GET", ENDPOINT + "/v1/models"):
            if call.headers != {"Authorization": "Bearer " + self.keys[-1]}:
                return 401, {"type": "error", "error": {"type": "invalid_key"}}
            # The gateway's answer: every pool's model name, the default one marked, not the first.
            names = [self.other, self.default]
            return 200, {
                "object": "list",
                "data": [
                    {"id": name, "object": "model", "type": "model", "default": name == self.default} for name in names
                ],
                "default": self.default,
            }
        return 404, None


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A scratch home folder that holds both harnesses' settings, with settings of the student's own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(CLAUDE_CODE_BEFORE), encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(CODEX_CONFIG, encoding="utf-8")
    (tmp_path / ".codex" / "rhi.config.toml").write_text(CODEX_BEFORE, encoding="utf-8")
    before = keyring.get_keyring()
    keyring.set_keyring(MemoryKeychain())
    yield tmp_path
    keyring.set_keyring(before)


def log_in(endpoint: Endpoint, address: str | None = ENDPOINT) -> str:
    """Log in against the stub; what the command printed."""
    out = io.StringIO()
    login.login(address, transport=endpoint, sleep=lambda seconds: None, clock=lambda: 0.0, out=out)
    assert store.load_key() == endpoint.keys[-1]
    return out.getvalue()


def claude_code(home: Path) -> dict[str, Any]:
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def codex(home: Path) -> dict[str, Any]:
    return tomllib.loads((home / ".codex" / "rhi.config.toml").read_text(encoding="utf-8"))


def test_claude_code_is_given_the_helper_and_the_endpoint(home: Path) -> None:
    log_in(Endpoint())
    settings = claude_code(home)
    assert settings.pop("apiKeyHelper") == "rhi token"
    assert settings["env"].pop("ANTHROPIC_BASE_URL") == ENDPOINT
    assert settings == CLAUDE_CODE_BEFORE


def test_codex_is_given_a_provider_profile_for_the_endpoint(home: Path) -> None:
    endpoint = Endpoint()
    log_in(endpoint)
    profile = codex(home)
    assert (profile.pop("model_provider"), profile.pop("model")) == ("rhi", endpoint.default)
    provider = profile.pop("model_providers")["rhi"]
    assert provider["base_url"] == ENDPOINT + "/v1"
    assert provider["wire_api"] == "responses"
    assert (provider["auth"]["command"], provider["auth"]["args"]) == ("rhi", ["token"])
    # No stream timeout: Codex keeps its own default.
    assert set(provider) == {"name", "base_url", "wire_api", "auth"}
    assert profile == tomllib.loads(CODEX_BEFORE)
    assert "# kept" in (home / ".codex" / "rhi.config.toml").read_text(encoding="utf-8")
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8") == CODEX_CONFIG


def test_both_files_are_made_when_a_harness_has_none(home: Path) -> None:
    (home / ".claude" / "settings.json").unlink()
    (home / ".codex" / "rhi.config.toml").unlink()
    endpoint = Endpoint()
    log_in(endpoint)
    assert claude_code(home) == {"apiKeyHelper": "rhi token", "env": {"ANTHROPIC_BASE_URL": ENDPOINT}}
    profile = codex(home)
    assert (profile["model_provider"], profile["model"]) == ("rhi", endpoint.default)
    assert set(profile) == {"model_provider", "model", "model_providers"}
    assert "stream_idle_timeout_ms" not in profile["model_providers"]["rhi"]


# A key as TOML lets a file write it: bare, or between quotes of either kind.
WRITTEN = ("{}", '"{}"', "'{}'")


@pytest.mark.parametrize("key", [form.format("stream_idle_timeout_ms") for form in WRITTEN])
def test_a_login_removes_the_stream_timeout_an_earlier_login_wrote_and_keeps_a_student_s_own(
    home: Path, key: str
) -> None:
    endpoint = Endpoint()
    log_in(endpoint)
    path = home / ".codex" / "rhi.config.toml"
    written = path.read_text(encoding="utf-8")
    assert written.count('wire_api = "responses"\n') == 1

    def with_timeout(milliseconds: int) -> str:
        line = f"{key} = {milliseconds}\n"
        return written.replace('wire_api = "responses"\n', 'wire_api = "responses"\n' + line)

    # The profile as an earlier login left it, with the 15 minutes in the provider's table.
    path.write_text(with_timeout(900000), encoding="utf-8")
    assert codex(home)["model_providers"]["rhi"]["stream_idle_timeout_ms"] == 900000
    assert str(path) in log_in(endpoint)
    assert path.read_text(encoding="utf-8") == written

    # Another value was not written by a login: it stays, and the file is not written.
    path.write_text(with_timeout(600000), encoding="utf-8")
    assert str(path) not in log_in(endpoint)
    assert path.read_text(encoding="utf-8") == with_timeout(600000)


def test_the_key_is_in_no_file(home: Path) -> None:
    endpoint = Endpoint()
    log_in(endpoint)
    key = store.load_key()
    assert key and key == endpoint.keys[-1]
    files = [path for path in home.rglob("*") if path.is_file()]
    assert {home / ".claude" / "settings.json", home / ".codex" / "rhi.config.toml"} <= set(files)
    assert [path for path in files if key.encode() in path.read_bytes()] == []


def test_a_second_login_changes_no_setting(home: Path) -> None:
    endpoint = Endpoint()
    log_in(endpoint)
    files = [home / ".claude" / "settings.json", home / ".codex" / "rhi.config.toml"]
    first = [path.read_bytes() for path in files]
    log_in(endpoint, address=None)
    assert endpoint.keys[0] != endpoint.keys[1]
    assert [path.read_bytes() for path in files] == first


def test_the_login_prints_each_file_it_changed(home: Path) -> None:
    endpoint = Endpoint()
    files = [str(home / ".claude" / "settings.json"), str(home / ".codex" / "rhi.config.toml")]
    first = log_in(endpoint)
    assert all(path in first for path in files)
    second = log_in(endpoint)
    assert not any(path in second for path in files)

    endpoint.default = "model-" + secrets.token_hex(4)
    third = log_in(endpoint)
    assert [path in third for path in files] == [False, True]


@pytest.mark.parametrize("key", [form.format("model") for form in WRITTEN])
def test_a_new_default_model_changes_that_setting_and_keeps_what_the_student_added(home: Path, key: str) -> None:
    endpoint = Endpoint()
    log_in(endpoint)
    path = home / ".codex" / "rhi.config.toml"
    text = path.read_text(encoding="utf-8")
    assert "[model_providers.rhi]\n" in text and text.count("\nmodel = ") == 1
    text = text.replace("\nmodel = ", f"\n{key} = ")
    path.write_text(
        text.replace("[model_providers.rhi]\n", "[model_providers.rhi]  # mine\nrequest_max_retries = 2\n"),
        encoding="utf-8",
    )
    before = codex(home)

    endpoint.default = "model-" + secrets.token_hex(4)
    log_in(endpoint)
    before["model"] = endpoint.default
    assert codex(home) == before
    assert "# mine" in path.read_text(encoding="utf-8")


def test_a_file_the_login_cannot_change_is_left_as_it_is_and_named(home: Path) -> None:
    path = home / ".codex" / "rhi.config.toml"
    # The provider's helper as a dotted key: no line-by-line change of it leaves a file Codex can read.
    path.write_text('model_providers.rhi.auth = { command = "another" }\n' + CODEX_BEFORE, encoding="utf-8")
    before = path.read_bytes()
    endpoint = Endpoint()
    with pytest.raises(login.LoginError, match="rhi.config.toml"):
        log_in(endpoint)
    assert path.read_bytes() == before
    # The key is stored all the same, and the other harness is configured.
    assert store.load_key() == endpoint.keys[-1]
    assert claude_code(home)["apiKeyHelper"] == "rhi token"


def test_token_prints_the_stored_key_and_nothing_else(capsys: pytest.CaptureFixture[str]) -> None:
    assert main.main(["token"]) == 1
    without = capsys.readouterr()
    assert without.out == ""
    assert "rhi login" in without.err

    key = new_key()
    store.save_key(key)
    assert main.main(["token"]) == 0
    printed = capsys.readouterr()
    assert (printed.out, printed.err) == (key + "\n", "")
