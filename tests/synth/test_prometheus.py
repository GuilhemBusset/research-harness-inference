"""The metrics workspace (system design, sections 6.6 and 7.1): one, in the observability stack,
keeping samples 90 days and retained on deletion and on replacement, with a policy for remote
write and a policy for queries, each on that workspace only.

The CDK is imported inside the fixtures, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import aws_cdk as cdk

Resources = dict[str, dict[str, Any]]


@pytest.fixture(scope="module")
def observability(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> tuple[Any, Resources]:
    """The observability stack of the stage, and the resources of its template."""
    stage = app.node.find_child("Rhi")
    stack = stage.observability  # ty: ignore[missing-attribute]
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[missing-attribute]
    return stack, templates[path]["Resources"]


def of_type(resources: Resources, kind: str) -> Resources:
    return {logical_id: resource for logical_id, resource in resources.items() if resource["Type"] == kind}


def test_one_workspace_keeps_samples_90_days_and_is_retained(observability: tuple[Any, Resources]) -> None:
    stack, resources = observability
    [(logical_id, workspace)] = of_type(resources, "AWS::APS::Workspace").items()
    assert logical_id == stack.resolve(stack.get_logical_id(stack.prometheus.workspace))
    assert workspace["Properties"]["WorkspaceConfiguration"]["RetentionPeriodInDays"] == 90
    assert workspace["DeletionPolicy"] == "Retain"
    assert workspace["UpdateReplacePolicy"] == "Retain"


def test_a_write_policy_and_a_query_policy_name_that_workspace_only(observability: tuple[Any, Resources]) -> None:
    stack, resources = observability
    workspace_arn = {"Fn::GetAtt": [stack.resolve(stack.get_logical_id(stack.prometheus.workspace)), "Arn"]}
    policies = of_type(resources, "AWS::IAM::ManagedPolicy")
    assert len(policies) == 2
    actions = {}
    for name in ("write_policy", "query_policy"):
        logical_id = stack.resolve(stack.get_logical_id(getattr(stack.prometheus, name).node.default_child))
        statements = policies[logical_id]["Properties"]["PolicyDocument"]["Statement"]
        for statement in statements:
            assert statement["Effect"] == "Allow"
            assert statement["Resource"] == workspace_arn, name
        actions[name] = {
            action for s in statements for action in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
        }
    assert actions["write_policy"] == {"aps:RemoteWrite"}
    assert actions["query_policy"] == {"aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata"}
