"""Run the acceptance command with the repository's locked environment."""

import subprocess


def test_required_measurement_command():
    result = subprocess.run(
        ["uv", "run", "--locked", "pytest", "tests/unit/test_measurements.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
