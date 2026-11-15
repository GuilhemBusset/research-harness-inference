"""Independent checks of the owner's walkthrough; every service client is local."""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run_setup(tmp_path):
    tree = tmp_path / "checkout"
    tree.mkdir()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    for name in filter(None, tracked):
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target, follow_symlinks=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    calls.touch()
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import sys\nfrom rhi import owner_config\n"
        "raise SystemExit(owner_config.main(sys.argv[1:]))\n"
    )
    bodies = {
        "aws": "case \"$2\" in\nlist-service-quotas) echo 'L-EXAMPLE 0';;\n"
        "get-caller-identity) echo 123456789012;;\nget) echo us-east-1;;\nesac\n",
        "gh": "exit 91\n",
        "uv": "[ \"$1 $2 $3 $4 $5 $6\" = 'run --locked --no-dev python -m rhi.owner_config' ] || exit 92\n"
        "[ \"$7\" != write ] || exit 0\nshift 6\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(runner))} \"$@\"\n",
    }
    for name, body in bodies.items():
        tool = bin_dir / name
        tool.write_text('#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$PROBE_CALLS"\n' + body)
        tool.chmod(0o755)
    cdk = tree / "node_modules/.bin/cdk"
    cdk.parent.mkdir(parents=True)
    cdk.write_text('#!/bin/bash\nprintf "%s\\n" "cdk $*" >> "$PROBE_CALLS"\n')
    cdk.chmod(0o755)
    external = tmp_path / "config.toml"
    external.write_bytes((tree / "config.example.toml").read_bytes())

    def snapshot():
        return {str(p.relative_to(tree)): None if p.is_dir() else p.read_bytes() for p in tree.rglob("*")}

    def invoke(*args, answers="", file_text=None):
        if file_text is not None:
            external.write_text(file_text)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "AMAZON_", "CDK_DEFAULT_"))}
        env.update(PATH=f"{bin_dir}:{env['PATH']}", PROBE_CALLS=str(calls), TMPDIR=str(tmp_path))
        calls.write_text("")
        before = snapshot()
        result = subprocess.run(
            ["bash", "setup/owner-setup.sh", *map(str, args)], cwd=tree, env=env,
            input=answers, text=True, capture_output=True, check=False,
        )
        assert snapshot() == before
        return result, calls.read_text()

    return invoke, external, tree


def test_d1_d6_a1_all_steps_without_service_calls_or_tree_writes(run_setup):
    invoke, external, _ = run_setup
    result, calls = invoke("--dry-run", "--config", external)
    assert result.returncode == 0, result.stderr
    headings = re.findall(r"^== Step (\d) of 9: (.*) ==$", result.stdout, re.MULTILINE)
    assert [n for n, _ in headings] == list("123456789")
    for (_, title), expected in zip(headings, (
        "quotas", "domain", "Apps", "configuration", "billing", "Bootstrap", "connection", "ruleset", "roster"
    ), strict=True):
        assert expected.lower() in title.lower()
    assert "uv run --locked --no-dev python -m rhi.owner_config check" in calls
    assert " write " not in calls
    assert not any(f"/bin/{tool} " in calls for tool in ("aws", "gh"))
    for command in ("request-service-quota-increase", "owner_config write", "cloudformation deploy", "cdk bootstrap",
                    "apply-ruleset.sh", "gh repo edit", "gh api user"):
        assert command in result.stdout


def test_d1_a3_commands_require_confirmation(run_setup):
    invoke, _, _ = run_setup
    for answers, count in (("n\nn\nn\n", 0), ("n\nn\ny\nn\n", 1), ("n\nn\ny\ny\n", 2)):
        result, calls = invoke("--step", "quotas", "--max-instances", 2, answers=answers)
        assert result.returncode == 0, result.stderr
        assert len(calls.splitlines()) == count
    result, calls = invoke("--step", "bootstrap", answers="n\nn\n")
    assert result.returncode == 0
    assert calls == ""
    result, calls = invoke("--step", "repository", answers="n\nn\nn\n")
    assert result.returncode == 0
    assert calls == ""


def test_d4_a5_standalone_write_is_confirmed(run_setup):
    invoke, external, _ = run_setup
    result, calls = invoke("--step", "configuration", "--config", external, answers="y\nn\n")
    assert result.returncode == 0 and " check " in calls and " write " not in calls
    result, calls = invoke("--step", "configuration", "--config", external, answers="y\ny\nn\n")
    assert result.returncode == 0 and " write " in calls
    assert "/bin/aws " not in calls


def test_a4_a6_owner_instructions(run_setup):
    invoke, _, _ = run_setup
    domain, calls = invoke("--dry-run", "--step", "domain")
    assert domain.returncode == 0 and calls == ""
    assert "decision 10" in domain.stdout and "public hosted zone" in domain.stdout
    billing, calls = invoke("--dry-run", "--step", "billing")
    assert billing.returncode == 0 and calls == ""
    assert "Billing preferences" in billing.stdout and "CloudWatch billing" in billing.stdout
