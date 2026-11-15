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


@pytest.mark.parametrize("breach", ["pull-request", "webhook", "asset-location", "deployment-order"])
def test_each_required_breach_is_independently_refused(cdk, tmp_path, breach):
    policy = load_policy()
    assert policy.breaches(policy.built(cdk, tmp_path / "baseline")) == []

    def change(cdk, stack, pipeline):
        if breach == "pull-request":
            resource = pipeline.node.default_child
            action = pipeline.stages[0].actions[0].action_properties.action_name
            resource.add_property_override("Triggers", [{
                "ProviderType": "CodeStarSourceConnection",
                "GitConfiguration": {"SourceActionName": action, "PullRequest": [{"Events": ["OPEN"]}]},
            }])
        elif breach == "webhook":
            cdk.aws_codebuild.Project(
                stack, "IndependentWebhook",
                source=cdk.aws_codebuild.Source.git_hub(owner="owner", repo="repo", webhook=True),
            )
        elif breach == "asset-location":
            index = next(index for index, stage in enumerate(pipeline.stages) if stage.stage_name == "Assets")
            pipeline.node.default_child.add_property_override(f"Stages.{index}.Name", "Elsewhere")

    if breach == "asset-location":
        changes = {"stack_with_an_asset": policy.BOUNDARY, "change": change}
    elif breach == "deployment-order":
        changes = {"approval": "last"}
    else:
        changes = {"change": change}
    templates = policy.built(cdk, tmp_path / "breached", **changes)
    expected = {
        "pull-request": policy.ANOTHER_TRIGGER,
        "webhook": policy.WEBHOOK,
        "asset-location": policy.ASSETS_ELSEWHERE,
        "deployment-order": policy.NOT_AFTER_THE_APPROVAL,
    }
    assert expected[breach] in policy.refused(templates)
