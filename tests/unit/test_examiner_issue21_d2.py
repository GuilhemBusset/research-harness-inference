"""Independent empty-folder, runtime and skip checks against the gate."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("body", "runtime", "ci", "status"),
    [
        (None, False, True, 0),
        ("def test_ok():\n    pass\n", False, False, 1),
        ("import pytest\npytest.skip('module unavailable', allow_module_level=True)\n", True, True, 1),
        ("import pytest\n@pytest.mark.skip(reason='unavailable')\ndef test_it():\n    pass\n", True, True, 1),
        ("import pytest\n@pytest.mark.skip(reason='unavailable')\ndef test_it():\n    pass\n", True, False, 0),
        ("def test_bad():\n    assert False\n", True, True, 1),
    ],
)
def test_container_step_outcomes(tmp_path: Path, body: str | None, runtime: bool, ci: bool, status: int) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "scripts").mkdir()
    folder = tmp_path / "tests/container"
    folder.mkdir(parents=True)
    for name in ("scripts/check.sh", "pyproject.toml", "uv.lock", ".python-version"):
        (tmp_path / name).write_bytes((root / name).read_bytes())
    if body is not None:
        (folder / "test_outcome.py").write_text(body, encoding="utf-8")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(f"#!/bin/sh\nexit {0 if runtime else 1}\n", encoding="utf-8")
    docker.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "CI"}
    env["PATH"] = f"{binaries}{os.pathsep}{env['PATH']}"
    if ci:
        env["CI"] = "true"
    done = subprocess.run(
        ["bash", "scripts/check.sh", "container-tests"], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == status, done.stdout + done.stderr
    if body is None:
        assert "no test" in done.stdout
    elif not runtime:
        assert "no container runtime answers" in done.stderr
