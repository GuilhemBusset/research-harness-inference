"""Independent per-module scope check for default-role-policy acknowledgements."""

import importlib.util
import sys
from pathlib import Path


def load_check(filename):
    spec = importlib.util.spec_from_file_location(
        "issue31_" + filename.removesuffix(".py"), Path(__file__).resolve().parents[1] / "policy" / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_module(path, name, text, monkeypatch):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_acknowledgement_applies_only_to_its_own_module(cdk, read_app, read_acknowledged, tmp_path, monkeypatch):
    source = tmp_path / "source"
    first = source_module(
        source / "first.py", "probe31_first", "import aws_cdk as cdk\nclass Stack(cdk.Stack): pass\n", monkeypatch
    )
    second = source_module(
        source / "second.py", "probe31_second", "import aws_cdk as cdk\nclass Stack(cdk.Stack): pass\n", monkeypatch
    )
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    for name, module in (("First", first), ("Second", second)):
        role = cdk.aws_iam.Role(
            module.Stack(app, name), "Role", assumed_by=cdk.aws_iam.ServicePrincipal("ec2.amazonaws.com")
        )
        role.add_to_policy(cdk.aws_iam.PolicyStatement(actions=["ec2:DescribeInstances"], resources=["*"]))
    folder = tmp_path / "acknowledged"
    folder.mkdir()
    (folder / "first.toml").write_text('[wildcard_actions]\n"ec2:DescribeInstances" = "AWS requires every resource."\n')
    templates, modules = read_app(app, source)
    check = load_check("test_iam_wildcards.py")
    assert len(check.violations(templates, modules, {})) == 2
    found = check.violations(templates, modules, read_acknowledged(folder))
    assert len(found) == 1
    assert modules[found[0].template, found[0].logical_id] == "second"


_policy = load_check("conftest.py")
cdk = _policy.cdk
read_app = _policy.read_app
read_acknowledged = _policy.read_acknowledged
synthesized = _policy.synthesized
templates = _policy.templates
created_by = _policy.created_by
module_names = _policy.module_names
