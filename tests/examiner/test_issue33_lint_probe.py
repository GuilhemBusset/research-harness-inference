"""The setup template discovery actually includes and lints the bootstrap policies."""

import importlib.util
from pathlib import Path

from cfnlint.api import lint


def test_bootstrap_is_discovered_and_passes_cfn_lint():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("bootstrap_lint", root / "tests/synth/test_cfn_lint.py")
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    target = root / "setup/bootstrap/policies.yaml"
    assert target in checker.setup_templates(root / "setup")
    assert lint(target.read_text()) == []
