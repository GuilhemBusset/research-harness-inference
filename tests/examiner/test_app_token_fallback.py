"""D5: absent App configuration preserves the existing gh login and names it."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "tools/implementation-loop"))
from implement_loop import app_token, cli


def test_existing_login(monkeypatch, tmp_path):
    for name in app_token.VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_TOKEN", "existing-login-placeholder")
    before = dict(os.environ)
    cli.git_as_the_app()
    assert dict(os.environ) == before and app_token.gh_env() is None
    seen = []

    def run(args, **kwargs):
        seen.append(kwargs.get("env"))
        return subprocess.CompletedProcess(args, 0, "owner-placeholder", "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    status, _name, detail = cli.github_identity_check(tmp_path)
    assert status == "PASS" and "owner-placeholder" in detail and seen == [None]
    monkeypatch.setenv(app_token.VARS[0], "4242")
    assert cli.github_identity_check(tmp_path)[0] == "FAIL"
