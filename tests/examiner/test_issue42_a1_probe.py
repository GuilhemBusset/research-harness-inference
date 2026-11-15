"""Execute the acceptance command independently."""

import subprocess


def test_the_policy_module_passes_on_the_current_app():
    result = subprocess.run(
        ["uv", "run", "--locked", "pytest", "tests/policy/test_pipeline.py", "-q"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
