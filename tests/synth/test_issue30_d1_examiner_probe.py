"""Independent checks for issue 30's linting gate."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_dev_pin_resolves_to_the_installed_distribution():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    version = importlib.metadata.version("cfn-lint")
    assert f"cfn-lint=={version}" in project["dependency-groups"]["dev"]
    assert not any(req.startswith("cfn-lint") for req in project["project"]["dependencies"])
    assert [p["version"] for p in lock["package"] if p["name"] == "cfn-lint"] == [version]
