"""Independent execution of the exact acceptance command."""

import subprocess
from pathlib import Path


def test_required_policy_command_passes():
    result = subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "pytest",
            "tests/policy/test_acknowledged.py",
            "tests/policy/test_iam_wildcards.py",
            "tests/policy/test_trust.py",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
