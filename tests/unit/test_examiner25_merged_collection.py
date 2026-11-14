from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_merged_examiner_tests_still_collect() -> None:
    repo = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(repo / "pyproject.toml"),
         "--collect-only", "-q", "tests/examiner"],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
