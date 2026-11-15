"""Independently inspect the permissions the real Apps step prints."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_apps():
    done = subprocess.run(
        ["bash", "setup/owner-setup.sh", "--dry-run", "--step", "apps"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    login, reporter, factory = done.stdout.split("Permissions:")[1:]
    assert "none at all" in login and "Device Flow: on" in login and "login_client_id" in login
    assert "configuration" in login
    assert "Commit statuses, read and write. Nothing else" in reporter and "this repository alone" in reporter
    assert "Contents, Pull requests and Issues, read and write" in factory
    assert "Actions, read-only" in factory and "Workflows and Administration: no access" in factory
    assert "stays on the factory's host" in factory
    assert "https://github.com/settings/apps/new" in done.stdout
