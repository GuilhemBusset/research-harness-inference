"""Independent witnesses for the pipeline's trigger and approval rules."""

import importlib.util
import json
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


def test_a_logged_security_diff_is_not_a_permissions_check(cdk, tmp_path):
    policy = load_policy()

    def change(cdk, stack, _pipeline):
        projects = [node for node in stack.node.find_all() if isinstance(node, cdk.aws_codebuild.CfnProject)]
        for project in projects:
            resolved = stack.resolve(project.source)
            spec = resolved.get("buildSpec", resolved.get("BuildSpec"))
            if spec is not None and policy.SECURITY_DIFF in policy.text_of(spec):
                project.add_property_override(
                    "Source.BuildSpec",
                    json.dumps({"version": "0.2", "phases": {"build": {"commands": ["echo '" + policy.SECURITY_DIFF + "'"]}}}),
                )
                return
        raise AssertionError("No permissions check project was synthesized")

    templates = policy.built(cdk, tmp_path, change=change)
    assert policy.differences(templates, policy.BUILT) == []
    assert policy.NOT_AFTER_THE_APPROVAL in policy.refused(templates)
