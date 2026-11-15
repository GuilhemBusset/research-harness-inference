"""Independent template coverage and construct-module attribution evidence."""

import importlib.util
import sys
from pathlib import Path

import pytest


def source_module(path, name, text, monkeypatch):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_every_real_template_is_attributed(templates, created_by, module_names):
    assert templates and any("/" in path for path in templates)
    expected = {
        (path, logical_id) for path, template in templates.items() for logical_id in template.get("Resources", {})
    }
    assert expected == set(created_by)
    assert set(created_by.values()) <= module_names


def test_root_and_multiple_stage_stacks_and_nearest_module(cdk, read_app, tmp_path, monkeypatch):
    source = tmp_path / "source"
    root = source_module(
        source / "stage.py",
        "probe31_stage",
        "import aws_cdk as cdk\nclass Stage(cdk.Stage): pass\nclass Root(cdk.Stack): pass\n",
        monkeypatch,
    )
    pool = source_module(
        source / "pools" / "group.py",
        "probe31_pool",
        "from constructs import Construct\nclass Pool(Construct): pass\n",
        monkeypatch,
    )
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    pipeline = root.Root(app, "Pipeline")
    cdk.CfnResource(pipeline, "RootTopic", type="AWS::SNS::Topic")
    stage = root.Stage(app, "Endpoint")
    for name in ("First", "Second"):
        stack = cdk.Stack(stage, name)
        cdk.CfnResource(stack, "StageTopic", type="AWS::SNS::Topic")
        cdk.CfnResource(pool.Pool(stack, "Pool"), "PoolTopic", type="AWS::SNS::Topic")
    templates, creators = read_app(app, source)
    assert len(templates) == 3
    assert sorted(creators.values()) == ["pools_group", "pools_group", "stage", "stage", "stage"]
    assert sum("/" in path for path in templates) == 2


def test_unattributed_resource_is_rejected(cdk, read_app, tmp_path):
    app = cdk.App(outdir=str(tmp_path / "assembly"))
    cdk.CfnResource(cdk.Stack(app, "Unowned"), "Topic", type="AWS::SNS::Topic")
    with pytest.raises(AssertionError):
        read_app(app, tmp_path / "empty-source")


def load_check(filename):
    spec = importlib.util.spec_from_file_location(
        "issue31_fixture", Path(__file__).resolve().parents[1] / "policy" / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_policy = load_check("conftest.py")
cdk = _policy.cdk
read_app = _policy.read_app
read_acknowledged = _policy.read_acknowledged
synthesized = _policy.synthesized
templates = _policy.templates
created_by = _policy.created_by
module_names = _policy.module_names
