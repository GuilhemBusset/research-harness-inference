"""Independent execution of the explicit container gate step."""

import os
import subprocess
from pathlib import Path


def test_container_step_runs_a_real_test(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "scripts").mkdir()
    folder = tmp_path / "tests/container"
    folder.mkdir(parents=True)
    for name in ("scripts/check.sh", "pyproject.toml", "uv.lock", ".python-version"):
        (tmp_path / name).write_bytes((root / name).read_bytes())
    (folder / "test_witness.py").write_text(
        "from pathlib import Path\ndef test_runs():\n    Path('executed').touch()\n", encoding="utf-8"
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("#!/bin/sh\n[ \"$1\" = info ]\n", encoding="utf-8")
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f"{binaries}{os.pathsep}{os.environ['PATH']}", CI="true")
    done = subprocess.run(
        ["bash", "scripts/check.sh", "container-tests"], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "executed").exists()
    assert "uv run --locked pytest" in (root / "scripts/check.sh").read_text(encoding="utf-8")
