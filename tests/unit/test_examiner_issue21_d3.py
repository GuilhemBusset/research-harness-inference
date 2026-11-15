"""The checked-in pytest configuration separates container collection."""

import subprocess
import sys
import tomllib
from pathlib import Path


def test_default_collection_and_parser_pin(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    # An entry that includes another group is a table, not a requirement.
    requirements = [entry for entry in project["dependency-groups"]["dev"] if isinstance(entry, str)]
    pins = [entry for entry in requirements if entry.lower().startswith("pyyaml==")]
    assert len(pins) == 1
    version = pins[0].split("==")[1]
    locked = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    assert any(package["name"] == "pyyaml" and package["version"] == version for package in locked["package"])
    (tmp_path / "pyproject.toml").write_bytes((root / "pyproject.toml").read_bytes())
    for name in ("unit", "container"):
        folder = tmp_path / "tests" / name
        folder.mkdir(parents=True)
        (folder / f"test_{name}.py").write_text("def test_collect():\n    pass\n", encoding="utf-8")
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    default = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    explicit = subprocess.run(command + ["tests/container"], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert default.returncode == explicit.returncode == 0
    assert "test_unit.py::test_collect" in default.stdout
    assert "test_container.py::test_collect" not in default.stdout
    assert "test_container.py::test_collect" in explicit.stdout
