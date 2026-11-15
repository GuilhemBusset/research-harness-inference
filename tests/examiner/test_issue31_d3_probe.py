"""Independent acknowledgement-format checks."""

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


import pytest


@pytest.mark.parametrize(
    "name,text",
    [
        ("missing_module.toml", ""),
        ("stage.toml", '[other]\nkey = "A full sentence here."\n'),
        ("stage.toml", '[nag]\n"AwsSolutions-S1" = ""\n'),
        ("stage.toml", '[nag]\n"AwsSolutions-S1" = "label"\n'),
    ],
)
def test_acknowledgements_fail_closed(tmp_path, module_names, name, text):
    (tmp_path / name).write_text(text)
    check = load_check("test_acknowledged.py")
    assert check.violations(tmp_path, module_names)


def test_sentence_with_allowed_tables_is_admitted(tmp_path, module_names):
    (tmp_path / "stage.toml").write_text(
        '[wildcard_actions]\n"ec2:DescribeInstances" = "AWS requires every resource."\n'
    )
    assert load_check("test_acknowledged.py").violations(tmp_path, module_names) == []


_policy = load_check("conftest.py")
cdk = _policy.cdk
read_app = _policy.read_app
read_acknowledged = _policy.read_acknowledged
synthesized = _policy.synthesized
templates = _policy.templates
created_by = _policy.created_by
module_names = _policy.module_names
