import subprocess


def test_hygiene_gate_accepts_the_tree():
    result = subprocess.run(["scripts/check.sh", "hygiene"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
