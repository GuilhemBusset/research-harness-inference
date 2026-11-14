from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_fast_gate_passes_without_pythonpath() -> None:
    repo = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        ["scripts/check.sh", "fast"],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
