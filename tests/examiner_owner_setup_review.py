"""Independent owner walkthrough checks; service commands use local recording doubles."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from rhi import config, owner_config

ROOT = Path(__file__).resolve().parents[1]


def snapshot(tree: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(tree)): None if p.is_dir() else p.read_bytes() for p in tree.rglob("*")}


@dataclass
class Setup:
    tree: Path
    file: Path
    log: Path
    env: dict[str, str]

    def run(self, *args: str, answers: str = "") -> subprocess.CompletedProcess[str]:
        before = snapshot(self.tree)
        self.log.write_text("")
        result = subprocess.run(
            ["bash", "setup/owner-setup.sh", *args], cwd=self.tree, env=self.env,
            input=answers, text=True, capture_output=True, check=False,
        )
        assert snapshot(self.tree) == before
        return result

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    tree = tmp_path / "checkout"
    shutil.copytree(
        ROOT, tree,
        ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "cdk.out", "__pycache__", ".*_cache"),
    )
    file = tmp_path / "owner.toml"
    file.write_bytes((tree / "config.example.toml").read_bytes())
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "calls"
    uv = shutil.which("uv")
    assert uv is not None
    bodies = {
        "aws": '[ "$2" != list-service-quotas ] || printf "L-EXAMPLE\\t0\\n"\n',
        "gh": "exit 0\n",
        "cdk": "exit 0\n",
        "npm": "exit 0\n",
        # Only the writing command is recorded rather than executed. Checks use real uv and
        # the real loader, including uv's normal creation of a project environment.
        "uv": '[ "$7" != write ] || exit 0\n' + f'exec {shlex.quote(uv)} "$@"\n',
    }
    for name, body in bodies.items():
        path = tools / name
        path.write_text('#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$OWNER_PROBE_LOG"\n' + body)
        path.chmod(0o755)
    cdk = tree / "node_modules/.bin/cdk"
    cdk.parent.mkdir(parents=True)
    shutil.copy2(tools / "cdk", cdk)
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH")
    }
    env.update(
        PATH=f"{tools}:{env['PATH']}", OWNER_PROBE_LOG=str(log), TMPDIR=str(tmp_path),
        HYGIENE_LOCAL_VALUES=str(tmp_path / "local-values"), UV_OFFLINE="1",
    )
    return Setup(tree, file, log, env)


def test_dry_run_order_commands_and_unchanged_copy(setup: Setup) -> None:
    result = setup.run("--dry-run", "--max-instances", "2", "--config", str(setup.file))
    assert result.returncode == 0, result.stderr
    headings = re.findall(r"^== Step (\d) of 9: (.*) ==$", result.stdout, re.MULTILINE)
    assert [number for number, _ in headings] == list("123456789")
    words = ("quota", "domain", "Apps", "configuration", "billing", "bootstrap", "connection", "ruleset", "roster")
    for (_, title), word in zip(headings, words, strict=True):
        assert word.lower() in title.lower()
    for command in (
        "request-service-quota-increase", "owner_config check", "owner_config write",
        "cloudformation deploy", "cdk bootstrap", "apply-ruleset.sh", "gh repo edit", "gh api user",
    ):
        assert command in result.stdout
    assert re.findall(r"--desired-value (\d+)", result.stdout) == ["24"]
    assert len(setup.calls()) == 1 and "owner_config check" in setup.calls()[0]
    assert not list(setup.tree.parent.glob("rhi-owner-setup.*"))


@pytest.mark.parametrize(("options", "amounts"), [
    ([], ["24"]), (["--upgrade-pool"], ["120"]), (["--xl-pool"], ["24", "192"]),
])
def test_quota_arithmetic_and_optional_p_family(setup: Setup, options: list[str], amounts: list[str]) -> None:
    result = setup.run("--dry-run", "--step", "quotas", "--max-instances", "2", *options)
    assert result.returncode == 0, result.stderr
    assert re.findall(r"--desired-value (\d+)", result.stdout) == amounts
    assert ("Running On-Demand P instances" in result.stdout) == ("--xl-pool" in options)
    assert setup.calls() == []


def test_each_quota_command_needs_confirmation(setup: Setup) -> None:
    for answers, expected in (("n\nn\nn\n", 0), ("n\nn\ny\nn\n", 1), ("n\nn\ny\ny\n", 2)):
        result = setup.run("--step", "quotas", "--max-instances", "2", answers=answers)
        assert result.returncode == 0, result.stderr
        assert len(setup.calls()) == expected
    for step in ("bootstrap", "repository"):
        result = setup.run("--step", step, answers="n\nn\nn\nn\n")
        assert result.returncode == 0, result.stderr
        assert setup.calls() == []


def test_app_instructions(setup: Setup) -> None:
    result = setup.run("--dry-run", "--step", "apps")
    assert result.returncode == 0, result.stderr
    login, reporter, factory = re.split(r"\n[23]\. The ", result.stdout.split("1. The student login App", 1)[1])
    assert "Permissions: none at all" in login and "Enable Device Flow: on" in login
    assert "client ID goes into the configuration" in login and "login_client_id" in login
    assert "Permissions: Commit statuses, read and write. Nothing else." in reporter
    assert "Contents, Pull requests and Issues, read and write" in factory
    assert "Actions, read-only" in factory and "Administration: no access" in factory
    assert "private key stays on the factory's host" in factory
    assert "https://github.com/settings/apps/new" in result.stdout
    assert setup.calls() == []


def test_external_configuration_validation_and_confirmation(setup: Setup) -> None:
    result = setup.run("--dry-run", "--step", "configuration", "--config", str(setup.tree / "config.example.toml"))
    assert result.returncode != 0 and "inside the working tree" in result.stderr
    assert setup.calls() == []
    good = setup.file.read_text()
    setup.file.write_text(good.replace('domain = "example.com"', ""))
    result = setup.run("--dry-run", "--step", "configuration", "--config", str(setup.file))
    assert result.returncode != 0 and "domain" in result.stderr
    assert len(setup.calls()) == 1 and "owner_config check" in setup.calls()[0]
    setup.file.write_text(good)
    for answers, actions in (("n\ny\n", []), ("y\nn\n", ["check"]), ("y\ny\nn\n", ["check", "write"])):
        result = setup.run("--step", "configuration", "--config", str(setup.file), answers=answers)
        assert result.returncode == 0, result.stderr
        assert [call.split("rhi.owner_config ")[1].split()[0] for call in setup.calls()] == actions


class SSM:
    def __init__(self) -> None:
        self.parameters: list[dict[str, Any]] = []

    def put_parameter(self, **request: Any) -> None:
        assert request["Overwrite"] is True and request["Type"] == "String"
        self.parameters.append({k: request[k] for k in ("Name", "Value", "Type")})

    def get_parameters_by_path(self, **request: Any) -> dict[str, Any]:
        assert request == {"Path": "/rhi/config/", "Recursive": True}
        return {"Parameters": self.parameters}


def test_configuration_writer_round_trip_and_refusal(tmp_path: Path) -> None:
    file = tmp_path / "owner.toml"
    file.write_bytes((ROOT / "config.example.toml").read_bytes())
    client = SSM()
    assert owner_config.main(["check", str(file)], client) == 0
    assert client.parameters == []
    assert owner_config.main(["write", str(file)], client) == 0
    loaded = config.load({"RHI_CONFIG": "file", "RHI_CONFIG_FILE": str(file)})
    assert config.read_ssm(client) == loaded
    assert loaded.domain == "example.com"
    parameters = {p["Name"]: p["Value"] for p in client.parameters}
    assert parameters["/rhi/config/session_windows"] == "[]"
    assert not any(name.startswith("/rhi/config/pools/") for name in parameters)
    before = list(client.parameters)
    file.write_text('domain = "example.com"\n')
    assert owner_config.main(["write", str(file)], client) == 1
    assert client.parameters == before


def test_domain_and_billing_owner_guidance(setup: Setup) -> None:
    domain = setup.run("--dry-run", "--step", "domain")
    assert domain.returncode == 0 and setup.calls() == []
    assert "decision 10" in domain.stdout and "public hosted zone" in domain.stdout
    billing = setup.run("--dry-run", "--step", "billing")
    assert billing.returncode == 0 and setup.calls() == []
    assert "Billing preferences" in billing.stdout and "CloudWatch billing" in billing.stdout
