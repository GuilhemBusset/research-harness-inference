"""Independent checks of the Apps walkthrough, without service calls."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SECRET = "rhi/reporter/github-app"
MARK = "independent-example-key-text"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def shell(tmp_path):
    tree = tmp_path / "tree"
    shutil.copytree(REPO, tree, ignore=shutil.ignore_patterns(
        ".git", ".venv", "node_modules", "cdk.out", "__pycache__", ".*_cache"))
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls"
    calls.touch()
    for name in ("aws", "uv", "gh", "cdk", "npm"):
        body = '#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$PROBE_CALLS"\n'
        if name == "gh":
            body += 'printf "%s" "$PROBE_APP" | jq -r "$4"\n'
        path = tools / name
        path.write_text(body)
        path.chmod(0o755)
    key = tmp_path / "reporter.pem"
    key.write_text(MARK + "\nsecond line\n")
    temp = tmp_path / "temp"
    temp.mkdir()

    def snapshot():
        return {str(p.relative_to(tree)): ("dir" if p.is_dir() else p.read_bytes())
                for p in tree.rglob("*")}

    def run(*args, answers="", permissions=None):
        calls.write_text("")
        env = {k: v for k, v in os.environ.items()
               if k not in {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"}}
        env.update(PATH=str(tools) + os.pathsep + env["PATH"], TMPDIR=str(temp),
                   PROBE_CALLS=str(calls), PROBE_APP=json.dumps({"id": 707, "permissions": permissions or {}}))
        before = snapshot()
        done = subprocess.run(["bash", "setup/owner-setup.sh", "--step", "apps", *map(str, args)],
                              cwd=tree, env=env, input=answers, text=True, capture_output=True, check=False)
        assert snapshot() == before
        assert MARK not in done.stdout + done.stderr
        return done, calls.read_text()

    return tree, key, run


@pytest.mark.parametrize(("which", "permissions", "valid"), [
    ("login", {}, True),
    ("login", {"issues": "read"}, False),
    ("reporter", {"metadata": "read", "statuses": "write"}, True),
    ("reporter", {"statuses": "read"}, False),
    ("factory", {"metadata": "read", "actions": "read", "checks": "read", "contents": "write",
                 "issues": "write", "pull_requests": "write", "statuses": "read"}, True),
    ("factory", {"contents": "write", "administration": "write"}, False),
])
def test_d1_readback_and_permission_verdict(shell, which, permissions, valid):
    _, _, run = shell
    answers = {"login": "example-login\ny\n\n\n", "reporter": "\nexample-reporter\ny\n\n\n",
               "factory": "\n\nexample-factory\ny\n"}[which]
    done, calls = run(answers=answers, permissions=permissions)
    assert done.returncode == 0, done.stderr
    assert f"gh api apps/example-{which} --jq" in calls
    assert "ID: 707" in done.stdout
    for name, access in permissions.items():
        assert f"{name}={access}" in done.stdout
    assert ("These are the permissions of section 8.6." in done.stdout) == valid
    assert ("These are NOT the permissions of section 8.6" in done.stdout) != valid


def test_d2_walkthrough_routes_read_app_id_and_asks_for_key(shell):
    _, key, run = shell
    prefix = f"\nexample-reporter\ny\n808\n{key}\n"
    skipped, calls = run(answers=prefix + "n\n\n", permissions={"statuses": "write"})
    assert skipped.returncode == 0, skipped.stderr
    assert "rhi.owner_secret" not in calls
    done, calls = run(answers=prefix + "y\nn\n\n", permissions={"statuses": "write"})
    assert done.returncode == 0, done.stderr
    assert "installation ID on this repository" in done.stdout
    assert "private key file, outside the working tree" in done.stdout
    assert f"rhi.owner_secret {SECRET} --app-id 707 --installation-id 808 --private-key-file {key}" in calls
    assert "/aws " not in calls
