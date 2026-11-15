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


def test_reviewed_stages_and_actions_are_ordered(cdk, tmp_path):
    policy = load_policy()
    expected = [
        ("Source", [("owner_repo", "Source/AWS/CodeStarSourceConnection")]),
        ("Build", [("Synth", "Build/AWS/CodeBuild")]),
        ("UpdatePipeline", [("SelfMutate", "Build/AWS/CodeBuild")]),
        ("Rhi", [("Check.Check", "Build/AWS/CodeBuild"), ("Check.Confirm", "Approval/AWS/Manual")]),
    ]
    templates = policy.built(cdk, tmp_path)
    assert policy.differences(templates, expected) == []
    assert policy.differences(templates, list(reversed(expected)))
    assert policy.differences(templates, expected[:-1])
    assert policy.differences(templates, expected + [("Unreviewed", [])])
    retyped = [*expected[:2], ("UpdatePipeline", [("SelfMutate", "Approval/AWS/Manual")]), expected[3]]
    assert policy.differences(templates, retyped)
