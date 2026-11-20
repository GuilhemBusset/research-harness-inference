"""The source tree of the system design (section 8.1), as pytest, Ruff and ty see it.

Every Python root exists from the start and the three tools are pointed at all of them, so an
issue that adds a module to a package touches no configuration file.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]

# Import name -> the root its package lives under.
PACKAGES = {
    "rhi": "src",
    "rhi_common": "services/common",
    "rhi_gateway": "services/gateway",
    "rhi_issuer": "services/issuer",
    "rhi_scaler": "services/scaler",
    "rhi_reporter": "services/reporter",
    "rhi_lifecycle": "services/engine/lifecycle",
    "rhi_mock_engine": "services/engine/mock",
    "rhi_cli": "client",
    "rhi_admin": "admin",
    "rhi_dashboards": "dashboards",
}
# scripts/pipeline and spikes hold modules the tests import by name, not a package.
ROOTS = [*PACKAGES.values(), "scripts/pipeline", "spikes"]


def read_toml(name: str) -> dict[str, Any]:
    return tomllib.loads((REPO / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(("name", "root"), PACKAGES.items(), ids=PACKAGES)
def test_each_package_imports(name: str, root: str) -> None:
    # Only the repository's pytest configuration puts the roots on the path: nothing here does.
    module = importlib.import_module(name)
    assert module.__file__ == str(REPO / root / name / "__init__.py")


def test_the_tools_list_the_same_roots() -> None:
    pytest_path = read_toml("pyproject.toml")["tool"]["pytest"]["ini_options"]["pythonpath"]
    ruff_src = read_toml("ruff.toml")["src"]
    ty = read_toml("ty.toml")

    assert set(ROOTS) <= set(pytest_path)
    for folder in pytest_path:
        assert (REPO / folder).is_dir(), folder  # ty refuses a root that does not exist
        assert folder in ruff_src, folder
        assert folder in ty["environment"]["root"], folder
    for checked in ("app.py", "tests", *ROOTS):
        assert checked in ty["src"]["include"], checked


def test_the_spikes_are_imported_by_name_and_hold_no_test() -> None:
    module = importlib.import_module("common")
    assert module.__file__ == str(REPO / "spikes" / "common.py")
    # Their tests are under tests/unit/spikes: pytest is pointed at no folder of spikes/.
    testpaths = read_toml("pyproject.toml")["tool"]["pytest"]["ini_options"]["testpaths"]
    assert not any(Path(path).parts[0] == "spikes" for path in testpaths)
    assert not [path.name for path in (REPO / "spikes").rglob("test_*.py")]


def test_same_named_test_files_both_run(tmp_path: Path) -> None:
    for folder in ("first", "second"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "test_same.py").write_text(
            "from pathlib import Path\n\n\ndef test_runs():\n    Path(__file__).with_name('ran').touch()\n",
            encoding="utf-8",
        )

    # A second pytest, under the repository's configuration, as later issues' test files will run.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(REPO / "pyproject.toml"), "-p", "no:cacheprovider", str(tmp_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "first" / "ran").exists()
    assert (tmp_path / "second" / "ran").exists()
