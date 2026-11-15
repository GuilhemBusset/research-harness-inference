"""Independent examination of the pipeline invariant."""

import importlib.util
from pathlib import Path

import pytest


def load_policy():
    path = Path(__file__).resolve().parents[1] / "policy" / "test_pipeline.py"
    spec = importlib.util.spec_from_file_location("issue42_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cdk(monkeypatch):
    monkeypatch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
    import aws_cdk

    return aws_cdk


def test_added_asset_stack_keeps_list_but_needs_boundary(cdk, tmp_path):
    policy = load_policy()
    reviewed = policy.steps(policy.built(cdk, tmp_path / "original"))
    bounded = policy.built(cdk, tmp_path / "bounded", stack_with_an_asset=policy.BOUNDARY)
    assert policy.differences(bounded, reviewed) == []
    assert policy.breaches(bounded) == []
    roles = policy.resources(bounded, "AWS::IAM::Role")
    assert any("Other" in path for path, _, _, _ in roles)
    for path, logical_id, _, _ in roles:
        if "Other" in path:
            del bounded[path]["Resources"][logical_id]["Properties"]["PermissionsBoundary"]
    assert policy.differences(bounded, reviewed) == []
    assert policy.NO_BOUNDARY in policy.refused(bounded)
