"""D4/A2: gh gets the token; agent and check subprocesses cannot get it or the key."""
import os
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "tools/implementation-loop"))
from implement_loop import app_token, sandbox


def test_process_boundary(tmp_path, monkeypatch):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    key = home / "key"
    key.write_text(secrets.token_hex(64))
    token = secrets.token_hex(24)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv(app_token.VARS[2], str(key))
    monkeypatch.setattr(app_token, "token", lambda env=None: token)
    env = app_token.gh_env()
    assert env is not None and env["GH_TOKEN"] == token and "GH_TOKEN" not in os.environ
    script = ("import os,pathlib; assert 'GH_TOKEN' not in os.environ; "
              "assert not pathlib.Path(os.environ['LOOP_GITHUB_APP_KEY_FILE']).exists()")
    for vendor in (None, "claude", "codex"):
        cleaned = sandbox.clean_env(env, work / "no-gh", vendor)
        assert token not in cleaned.values()
        command = sandbox.Bubblewrap(home=home).command(["/usr/bin/python3", "-c", script], work,
                    vendor=vendor, mounts=sandbox.Mounts(writable=(work,)))
        process = subprocess.run(command, env=cleaned, capture_output=True, text=True, check=False)
        assert process.returncode == 0, process.stderr
