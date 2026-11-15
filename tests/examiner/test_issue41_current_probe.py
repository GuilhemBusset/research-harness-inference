"""Independent Apps walkthrough checks with local CLI and Secrets Manager doubles."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SECRET = "rhi/reporter/github-app"
KEY_TEXT = "example reporter material\nsecond example line\n"
CLI = '''import json
import os
import subprocess
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
with Path(os.environ["PROBE_CALLS"]).open("a") as output:
    output.write(json.dumps([tool, *sys.argv[1:]]) + "\\n")
if tool == "gh":
    assert sys.argv[1] == "api"
    app = json.loads(os.environ["PROBE_APPS"])[sys.argv[2]]
    subprocess.run(["jq", "-r", sys.argv[4]], input=json.dumps(app), text=True, check=True)
elif tool == "uv":
    assert sys.argv[1:7] == ["run", "--locked", "--no-dev", "python", "-m", "rhi.owner_secret"]
    import boto3
    import botocore.exceptions
    from rhi import owner_secret

    def forbidden(*args, **kwargs):
        raise AssertionError("real service client requested")

    boto3.client = forbidden

    class Secrets:
        def create_secret(self, **request):
            if os.environ["PROBE_EXISTS"] == "1":
                raise botocore.exceptions.ClientError(
                    {"Error": {"Code": "ResourceExistsException"}}, "CreateSecret")
            self.record("create", request)

        def put_secret_value(self, **request):
            self.record("update", request)

        def record(self, operation, request):
            with Path(os.environ["PROBE_WRITES"]).open("a") as output:
                output.write(json.dumps([operation, request]) + "\\n")

    sys.exit(owner_secret.main(sys.argv[7:], Secrets()))
else:
    raise AssertionError("unexpected external command: " + tool)
'''


class AppsRun:
    def __init__(self, folder: Path) -> None:
        self.tree = folder / "checkout"
        shutil.copytree(
            REPO,
            self.tree,
            ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "cdk.out", "__pycache__", ".*_cache"),
        )
        self.key = folder / "reporter.pem"
        self.key.write_text(KEY_TEXT)
        self.calls = folder / "calls.jsonl"
        self.writes = folder / "writes.jsonl"
        tools = folder / "bin"
        tools.mkdir()
        for name in ("gh", "aws", "uv", "cdk", "npm"):
            executable = tools / name
            executable.write_text(f"#!{sys.executable}\n{CLI}")
            executable.chmod(0o755)
        temp = folder / "temp"
        temp.mkdir()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"}
        }
        self.env.update(
            PATH=str(tools) + os.pathsep + self.env["PATH"],
            TMPDIR=str(temp),
            PROBE_CALLS=str(self.calls),
            PROBE_WRITES=str(self.writes),
            PROBE_EXISTS="0",
        )

    def snapshot(self) -> dict[str, bytes | None]:
        return {
            str(path.relative_to(self.tree)): None if path.is_dir() else path.read_bytes()
            for path in self.tree.rglob("*")
        }

    def run(
        self, *arguments: str, answers: str = "", permissions: list[dict[str, str]] | None = None
    ) -> subprocess.CompletedProcess[str]:
        rights = permissions if permissions is not None else [{}, {"statuses": "write"}, {}]
        apps = {
            f"apps/example-{name}": {"id": app_id, "permissions": given}
            for name, app_id, given in zip(("login", "reporter", "factory"), (211, 322, 433), rights, strict=True)
        }
        self.env["PROBE_APPS"] = json.dumps(apps)
        self.calls.write_text("")
        self.writes.write_text("")
        before = self.snapshot()
        result = subprocess.run(
            ["bash", "setup/owner-setup.sh", "--step", "apps", *arguments],
            cwd=self.tree,
            env=self.env,
            input=answers,
            capture_output=True,
            text=True,
            check=False,
        )
        assert self.snapshot() == before
        assert KEY_TEXT.splitlines()[0] not in result.stdout + result.stderr
        return result


@pytest.fixture
def apps_run(tmp_path: Path) -> AppsRun:
    return AppsRun(tmp_path)


@pytest.mark.parametrize("correct", [True, False])
def test_readback_of_all_three_apps(apps_run: AppsRun, correct: bool) -> None:
    permissions = [
        {},
        {"metadata": "read", "statuses": "write"},
        {
            "metadata": "read", "actions": "read", "checks": "read", "contents": "write",
            "issues": "write", "pull_requests": "write", "statuses": "read",
        },
    ]
    if not correct:
        for rights in permissions:
            rights["administration"] = "write"
    result = apps_run.run(
        answers="example-login\ny\nexample-reporter\ny\n\nexample-factory\ny\n", permissions=permissions
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in apps_run.calls.read_text().splitlines()]
    assert [call[:3] for call in calls] == [
        ["gh", "api", f"apps/example-{name}"] for name in ("login", "reporter", "factory")
    ]
    for app_id in (211, 322, 433):
        assert f"ID: {app_id}" in result.stdout
    assert "Its permissions: none" in result.stdout if correct else "administration=write" in result.stdout
    for rights in permissions:
        for name, access in rights.items():
            assert f"{name}={access}" in result.stdout
    assert result.stdout.count("These are the permissions of section 8.6.") == (3 if correct else 0)
    assert result.stdout.count("These are NOT the permissions of section 8.6") == (0 if correct else 3)


@pytest.mark.parametrize("exists", [False, True])
def test_reporter_prompt_to_secret_write(apps_run: AppsRun, exists: bool) -> None:
    apps_run.env["PROBE_EXISTS"] = "1" if exists else "0"
    result = apps_run.run(answers=f"\nexample-reporter\ny\n544\n{apps_run.key}\ny\nn\n\n")
    assert result.returncode == 0, result.stderr
    assert "installation ID on this repository" in result.stdout
    assert "private key file, outside the working tree" in result.stdout
    writes = [json.loads(line) for line in apps_run.writes.read_text().splitlines()]
    assert len(writes) == 1
    operation, request = writes[0]
    assert operation == ("update" if exists else "create")
    assert request["SecretId" if exists else "Name"] == SECRET
    assert json.loads(request["SecretString"]) == {
        "app_id": "322", "installation_id": "544", "private_key": KEY_TEXT,
    }


def test_dry_run_and_key_boundary(apps_run: AppsRun) -> None:
    inside = apps_run.tree / "example.pem"
    inside.write_text(KEY_TEXT)
    linked = apps_run.tree / "linked.pem"
    linked.symlink_to(apps_run.key)
    for key in (str(apps_run.key), str(inside), "example.pem", str(linked)):
        result = apps_run.run("--dry-run", "--reporter-key", key)
        assert apps_run.calls.read_text() == ""
        assert apps_run.writes.read_text() == ""
        if key == str(apps_run.key):
            assert result.returncode == 0, result.stderr
            assert result.stdout.count("$ gh api 'apps/<slug>'") == 3
            assert f"rhi.owner_secret {SECRET}" in result.stdout
            assert f"aws secretsmanager describe-secret --secret-id {SECRET}" in result.stdout
        else:
            assert result.returncode != 0
            assert "inside the working tree" in result.stderr
            assert "rhi.owner_secret" not in result.stdout
