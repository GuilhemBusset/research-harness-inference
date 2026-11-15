"""Independent bad-folder witnesses required by A4."""

import importlib.util
from pathlib import Path


def load_check(filename):
    spec = importlib.util.spec_from_file_location(
        "issue31_" + filename.removesuffix(".py"), Path(__file__).resolve().parents[1] / "policy" / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bad_module_and_missing_reason_are_rejected(tmp_path, module_names):
    check = load_check("test_acknowledged.py")
    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "nonexistent_module.toml").write_text("")
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "stage.toml").write_text('[wildcard_actions]\n"ec2:DescribeInstances" = ""\n')
    assert check.violations(missing, module_names)
    assert check.violations(empty, module_names)


_policy = load_check("conftest.py")
cdk = _policy.cdk
read_app = _policy.read_app
read_acknowledged = _policy.read_acknowledged
synthesized = _policy.synthesized
templates = _policy.templates
created_by = _policy.created_by
module_names = _policy.module_names
