"""D3: git receives the helper configuration, which answers with a fresh token."""
import io
import os
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "tools/implementation-loop"))
from implement_loop import app_token, cli, sandbox


def test_git_configuration_and_renewed_answer(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("GIT_CONFIG_"):
            monkeypatch.delenv(name)
    for name, value in zip(app_token.VARS, ("4242", "777", str(tmp_path / "key")), strict=True):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(app_token.API_VAR, raising=False)
    cli.git_as_the_app()
    configured = dict(os.environ)
    cli.git_as_the_app()
    assert dict(os.environ) == configured
    env = configured | sandbox.engine_git_env(configured, tmp_path / "no-hooks")
    config = subprocess.run(["git", "config", "--get-all", app_token.GIT_KEY], env=env,
                            capture_output=True, text=True, check=True)
    assert config.stdout.startswith("\n!") and "app_token.py" in config.stdout and "credential" in config.stdout
    now = [10000.0]
    monkeypatch.setattr(app_token, "_now", lambda: now[0])
    monkeypatch.setattr(app_token, "_held", [])
    monkeypatch.setattr(app_token, "mint", lambda app: app_token.Held(secrets.token_hex(24), now[0] + 3600, app))
    first = app_token.token()
    now[0] += 3060
    output = io.StringIO()
    assert app_token.credential("get", io.StringIO("protocol=https\nhost=github.com\n\n"),
                                output, io.StringIO()) == 0
    assert "username=x-access-token" in output.getvalue()
    assert "password=" + app_token._held[0].token in output.getvalue()
    assert app_token._held[0].token != first
    other = io.StringIO()
    app_token.credential("get", io.StringIO("protocol=https\nhost=example.com\n\n"), other, io.StringIO())
    assert not other.getvalue()
