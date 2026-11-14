"""Independent positive control on the current CI workflow."""

import runpy
from pathlib import Path

import yaml


def test_current_workflow_passes() -> None:
    root = Path(__file__).resolve().parents[2]
    actual = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert "required" in actual["jobs"] and len(actual["jobs"]) > 1
    module = runpy.run_path(str(root / "tests/unit/test_ci_workflow.py"))
    assert module["problems"](actual) == []
    module["test_ci_workflow_keeps_the_rules"]()
