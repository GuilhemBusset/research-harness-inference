"""The evaluation tool is in the checks everyone runs: pytest finds its tests with no path given,
and Ruff and ty resolve the package and check it (pyproject.toml, ruff.toml, ty.toml).

Run with no path (`uv run --locked pytest -k <this test>`), the test runs only if default
discovery reaches this folder.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
TOOL = "tools/evaluate"


def config(name: str) -> dict[str, Any]:
    return tomllib.loads((REPO / name).read_text(encoding="utf-8"))


def covers_tool(entries: list[str]) -> bool:
    """Whether a list of folders names the tool, or a folder above it."""
    return any(f"{TOOL}/".startswith(entry.rstrip("/*") + "/") for entry in entries)


def test_default_checks_cover_the_evaluation_tool() -> None:
    pytest_options = config("pyproject.toml")["tool"]["pytest"]["ini_options"]
    ruff = config("ruff.toml")
    ty = config("ty.toml")

    # Tests: collected by a pytest that is given no path.
    assert f"{TOOL}/tests" in pytest_options["testpaths"]
    assert not covers_tool(pytest_options["norecursedirs"])
    # Lint and types: `evaluate` is a package under `tools`, so that folder is the import root.
    assert "tools" in ruff["src"]
    assert "tools" in ty["environment"]["root"]
    assert covers_tool(ty["src"]["include"])
    # Neither tool leaves the folder out.
    assert not covers_tool(ruff.get("exclude", []) + ruff.get("extend-exclude", []))
    assert not covers_tool(ty["src"].get("exclude", []))
