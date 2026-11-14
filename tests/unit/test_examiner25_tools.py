from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


def test_all_tools_cover_layout() -> None:
    repo = Path(__file__).resolve().parents[2]
    roots = {
        "src", "services/common", "services/gateway", "services/issuer",
        "services/scaler", "services/reporter", "services/engine/lifecycle",
        "services/engine/mock", "client", "admin", "dashboards", "scripts/pipeline",
    }
    project = tomllib.loads((repo / "pyproject.toml").read_text())
    ruff = tomllib.loads((repo / "ruff.toml").read_text())
    ty = tomllib.loads((repo / "ty.toml").read_text())
    assert roots <= set(project["tool"]["pytest"]["ini_options"]["pythonpath"])
    assert roots <= set(ruff["src"])
    assert roots <= set(ty["environment"]["root"])
    assert roots | {"app.py", "tests"} <= set(ty["src"]["include"])
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--show-files", "."],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    discovered = {Path(line).resolve() for line in result.stdout.splitlines()}
    for root in roots - {"scripts/pipeline"}:
        for package in (repo / root).glob("*/__init__.py"):
            assert package in discovered
    assert repo / "tests/unit/test_layout.py" in discovered
