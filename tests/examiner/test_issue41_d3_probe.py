import os
import subprocess
from pathlib import Path


def test_dry_run_never_starts_remote_tools(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls"
    for name in ("gh", "aws", "uv", "cdk", "npm"):
        executable = tools / name
        executable.write_text('#!/bin/bash\nprintf called >> "$PROBE_CALLS"\nexit 79\n')
        executable.chmod(0o755)
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"], PROBE_CALLS=str(calls))
    script = Path(__file__).resolve().parents[2] / "setup/owner-setup.sh"
    done = subprocess.run(["bash", str(script), "--dry-run", "--step", "apps"],
                          env=env, text=True, input="", capture_output=True, check=False)
    assert done.returncode == 0, done.stderr
    assert not calls.exists()
    assert done.stdout.count("$ gh api 'apps/<slug>'") == 3
    assert "rhi.owner_secret rhi/reporter/github-app" in done.stdout
    assert "aws secretsmanager describe-secret --secret-id rhi/reporter/github-app" in done.stdout
