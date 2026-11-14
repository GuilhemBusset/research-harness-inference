from __future__ import annotations

import importlib
import runpy
from pathlib import Path

import pytest


def test_layout_test_imports_all_named_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path(__file__).resolve().parents[2]
    layout = runpy.run_path(str(repo / "tests/unit/test_layout.py"))
    expected = {
        "rhi", "rhi_common", "rhi_gateway", "rhi_issuer", "rhi_scaler",
        "rhi_reporter", "rhi_lifecycle", "rhi_mock_engine", "rhi_cli",
        "rhi_admin", "rhi_dashboards",
    }
    imported = set()
    original = importlib.import_module

    def record(name: str, package: str | None = None):
        imported.add(name)
        return original(name, package)

    monkeypatch.setattr(importlib, "import_module", record)
    for name, root in layout["PACKAGES"].items():
        layout["test_each_package_imports"](name, root)
    assert expected <= imported
