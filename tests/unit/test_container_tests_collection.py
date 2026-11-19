"""The tests under `tests/container/` are collected on a host with no container runtime.

Only CI's job `container-tests` can run them (tests/container/README.md). The agents' host must
still be able to read them: a file that needed the runtime to be imported, or that skipped itself
where the runtime is missing, could not be listed here, and would be found broken only in CI.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FOLDER = "tests/container"


def test_the_container_tests_are_collected_with_no_container_runtime(tmp_path: Path) -> None:
    files = sorted((REPO / FOLDER).glob("test_*.py"))
    assert files, f"{FOLDER} holds no test"
    # A PATH with no program on it: whatever this host has, the collection finds no `docker`.
    env = dict(os.environ) | {"PATH": str(tmp_path)}
    assert shutil.which("docker", path=env["PATH"]) is None

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", FOLDER],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert collected.returncode == 0, collected.stdout + collected.stderr
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
        assert names, f"{file.name} defines no test"
        for name in names:
            assert f"{FOLDER}/{file.name}::{name}" in collected.stdout, name
