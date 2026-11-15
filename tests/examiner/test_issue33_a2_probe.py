"""The actual setup linter must discover and lint the bootstrap template."""
import importlib.util
from pathlib import Path


def test_setup_linter_covers_bootstrap():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("setup_linter", root / "tests/synth/test_cfn_lint.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = root / "setup/bootstrap/policies.yaml"
    assert path in module.setup_templates(module.SETUP)
    assert not module.findings(path.read_text())
