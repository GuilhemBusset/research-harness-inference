"""Exercise fast/all dispatch with unrelated gate integrations isolated."""

import os
import subprocess
from pathlib import Path


def test_local_gate_never_imports_container_tests(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "scripts").mkdir()
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        (tmp_path / name).write_bytes((root / name).read_bytes())
    script = (root / "scripts/check.sh").read_text(encoding="utf-8")
    unrelated = [
        "hygiene", "static", "lock", "lint", "types", "engine_tests", "skills", "backlog_lint", "ship_tests", "synth",
    ]
    stubs = "\n".join(f"do_{name}() {{ :; }}" for name in unrelated)
    script = script.replace('case "$1" in', stubs + '\ncase "$1" in')
    (tmp_path / "scripts/check.sh").write_text(script, encoding="utf-8")
    for name in ("unit", "container"):
        (tmp_path / "tests" / name).mkdir(parents=True)
    (tmp_path / "tests/unit/test_safe.py").write_text("def test_safe():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests/container/test_forbidden.py").write_text(
        "from pathlib import Path\nPath('container-imported').touch()\nraise RuntimeError('container collected')\n",
        encoding="utf-8",
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("#!/bin/sh\ntouch runtime-contacted\nexit 1\n", encoding="utf-8")
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f"{binaries}{os.pathsep}{os.environ['PATH']}")
    for step in ("tests", "fast", "all"):
        done = subprocess.run(
            ["bash", "scripts/check.sh", step], cwd=tmp_path, env=env,
            capture_output=True, text=True, check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert not (tmp_path / "container-imported").exists()
        assert not (tmp_path / "runtime-contacted").exists()
