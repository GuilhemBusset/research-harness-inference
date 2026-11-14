from __future__ import annotations

import importlib
from pathlib import Path

import pytest

PACKAGES = [
    ("rhi", "src"),
    ("rhi_common", "services/common"),
    ("rhi_gateway", "services/gateway"),
    ("rhi_issuer", "services/issuer"),
    ("rhi_scaler", "services/scaler"),
    ("rhi_reporter", "services/reporter"),
    ("rhi_lifecycle", "services/engine/lifecycle"),
    ("rhi_mock_engine", "services/engine/mock"),
    ("rhi_cli", "client"),
    ("rhi_admin", "admin"),
    ("rhi_dashboards", "dashboards"),
]


@pytest.mark.parametrize(("name", "root"), PACKAGES)
def test_packages_are_empty_and_importable(name: str, root: str) -> None:
    repo = Path(__file__).resolve().parents[2]
    expected = repo / root / name / "__init__.py"
    assert expected.read_bytes() == b""
    filename = importlib.import_module(name).__file__
    assert filename is not None
    assert Path(filename).resolve() == expected
