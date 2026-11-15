"""D2: all gh entry points get a token renewed before the last ten minutes."""
import os
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "tools/implementation-loop"))
from implement_loop import app_token, cli


def test_gh_renewal(monkeypatch, tmp_path):
    for name, value in zip(app_token.VARS, ("4242", "777", str(tmp_path / "key")), strict=True):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(app_token.API_VAR, raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    now, seen = [10000.0], []
    monkeypatch.setattr(app_token, "_held", [])
    monkeypatch.setattr(app_token, "_now", lambda: now[0])
    monkeypatch.setattr(app_token, "mint", lambda app: app_token.Held(secrets.token_hex(24), now[0] + 3600, app))

    def run(args, **kwargs):
        seen.append(kwargs["env"]["GH_TOKEN"])
        return subprocess.CompletedProcess(args, 0, "{}", "")

    client = cli.github("o/r")
    client._run = run
    client.get_issue(1)
    first = seen[-1]
    now[0] += 2940
    client.get_issue(1)
    assert seen[-1] == first
    now[0] += 120
    client.get_issue(1)
    renewed = seen[-1]
    assert renewed != first
    monkeypatch.setattr(client, "_failed_runs", lambda sha: [{"id": 1}])
    client.failed_log("a" * 40)
    monkeypatch.setattr(cli.subprocess, "run", run)
    cli.repo_slug()
    cli._out(["gh", "auth", "status"])
    assert seen == [first, first, renewed, renewed, renewed, renewed]
    assert "GH_TOKEN" not in os.environ
