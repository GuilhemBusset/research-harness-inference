"""D4/A2: gh gets the token; agent and check subprocesses cannot get it or the key."""
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd() / "tools/implementation-loop"))
from implement_loop import app_token, sandbox


def _bubblewrap_works() -> bool:
    """The guard of the engine's own real-sandbox tests (`test_sandbox.py`). CI's `tests` job,
    which runs this folder, has no bubblewrap that may create namespaces; the loop's verifier
    has one, and there a skipped test fails the check."""
    exe = shutil.which("bwrap")
    if exe is None:
        return False
    try:
        return subprocess.run([exe, "--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
                              capture_output=True, timeout=60, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_bubblewrap = pytest.mark.skipif(not _bubblewrap_works(),
                                      reason="needs a bubblewrap that may create namespaces on this machine")


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    """(the environment of one `gh` process, the token in it, the home that holds the key, a worktree)."""
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
    return env, token, home, work


def test_process_boundary(boundary):
    env, token, _home, work = boundary
    for vendor in (None, "claude", "codex"):
        assert token not in sandbox.clean_env(env, work / "no-gh", vendor).values()


@needs_bubblewrap
def test_process_boundary_inside_the_sandbox(boundary):
    env, _token, home, work = boundary
    script = ("import os,pathlib; assert 'GH_TOKEN' not in os.environ; "
              "assert not pathlib.Path(os.environ['LOOP_GITHUB_APP_KEY_FILE']).exists()")
    for vendor in (None, "claude", "codex"):
        cleaned = sandbox.clean_env(env, work / "no-gh", vendor)
        command = sandbox.Bubblewrap(home=home).command(["/usr/bin/python3", "-c", script], work,
                    vendor=vendor, mounts=sandbox.Mounts(writable=(work,)))
        process = subprocess.run(command, env=cleaned, capture_output=True, text=True, check=False)
        assert process.returncode == 0, process.stderr
