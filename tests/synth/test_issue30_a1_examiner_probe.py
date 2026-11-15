"""Independent checks for issue 30's linting gate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_acceptance_command_succeeds():
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AWS_", "AMAZON_", "CDK_DEFAULT_")) and key != "VIRTUAL_ENV"
    }
    environment.update(
        AWS_EC2_METADATA_DISABLED="true", AWS_CONFIG_FILE="/dev/null",
        AWS_SHARED_CREDENTIALS_FILE="/dev/null", UV_NO_ENV_FILE="1",
    )
    result = subprocess.run(
        ["uv", "run", "--locked", "pytest", "tests/synth/test_cfn_lint.py"],
        cwd=REPO, env=environment, capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
