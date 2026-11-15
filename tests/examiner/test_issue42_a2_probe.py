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


def test_an_independent_extra_step_is_refused(cdk, tmp_path):
    policy = load_policy()
    original = policy.built(cdk, tmp_path / "original")
    reviewed = policy.steps(original)
    changed = policy.built(cdk, tmp_path / "changed", step=("IndependentVerification", ["true"]))
    assert policy.differences(original, reviewed) == []
    assert policy.differences(changed, reviewed) == [
        (policy.UNLISTED, "Rhi/IndependentVerification: Build/AWS/CodeBuild")
    ]
    assert policy.breaches(changed) == []
