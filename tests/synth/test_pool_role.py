"""A pool's instance role and its instances' log group (system design, section 4.3): the mock pool
of the example configuration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import aws_cdk as cdk

SESSION_ACTIONS = {
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r.get("Properties", {}) for name, r in template["Resources"].items() if r["Type"] == kind}


@pytest.fixture(scope="module")
def pools(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stage = app.node.find_child("Rhi")
    stack = stage.pools  # ty: ignore[unresolved-attribute] - the app's stage, typed as a construct
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]
    return templates[path]


@pytest.fixture(scope="module")
def role(pools: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The mock pool's instance role: the one role EC2 may assume."""
    (found,) = [
        (name, properties)
        for name, properties in of_type(pools, "AWS::IAM::Role").items()
        if any(
            "ec2.amazonaws.com" in as_list(statement.get("Principal", {}).get("Service", []))
            for statement in properties["AssumeRolePolicyDocument"]["Statement"]
        )
    ]
    return found


@pytest.fixture(scope="module")
def statements(pools: dict[str, Any], role: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every statement of the policies attached to the role."""
    name, properties = role
    assert "ManagedPolicyArns" not in properties
    assert "Policies" not in properties
    return [
        statement
        for policy in of_type(pools, "AWS::IAM::Policy").values()
        if {"Ref": name} in policy["Roles"]
        for statement in as_list(policy["PolicyDocument"]["Statement"])
    ]


def with_service(statements: list[dict[str, Any]], service: str) -> list[dict[str, Any]]:
    return [s for s in statements if any(action.startswith(f"{service}:") for action in as_list(s["Action"]))]


def test_the_role_is_the_mock_pools_and_the_launch_templates(
    pools: dict[str, Any], role: tuple[str, dict[str, Any]]
) -> None:
    name, properties = role
    assert {"Key": "rhi-pool", "Value": "mock"} in properties["Tags"]
    (template,) = of_type(pools, "AWS::EC2::LaunchTemplate").values()
    profile = template["LaunchTemplateData"]["IamInstanceProfile"]["Arn"]["Fn::GetAtt"][0]
    assert of_type(pools, "AWS::IAM::InstanceProfile")[profile]["Roles"] == [{"Ref": name}]


def test_writes_only_its_pools_heartbeat_rows(app: cdk.App, statements: list[dict[str, Any]]) -> None:
    stack = app.node.find_child("Rhi").pools  # ty: ignore[unresolved-attribute] - the app's stage, typed as a construct
    heartbeats = stack.resolve(stack.get_logical_id(stack.tables.heartbeats.node.default_child))
    (statement,) = with_service(statements, "dynamodb")
    assert statement["Effect"] == "Allow"
    assert sorted(as_list(statement["Action"])) == ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    assert statement["Resource"] == {"Fn::GetAtt": [heartbeats, "Arn"]}
    assert statement["Condition"] == {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["mock"]}}


def test_writes_only_its_own_log_group(pools: dict[str, Any], statements: list[dict[str, Any]]) -> None:
    (statement,) = with_service(statements, "logs")
    assert sorted(as_list(statement["Action"])) == ["logs:CreateLogStream", "logs:PutLogEvents"]
    group, attribute = statement["Resource"]["Fn::GetAtt"]
    assert attribute == "Arn"
    assert of_type(pools, "AWS::Logs::LogGroup")[group]["LogGroupName"] == "/rhi/pools/mock"


def test_writes_metrics_and_completes_its_own_groups_lifecycle_actions(statements: list[dict[str, Any]]) -> None:
    (metrics,) = with_service(statements, "aps")
    assert metrics["Action"] == "aps:RemoteWrite"
    assert "PrometheusWorkspace" in metrics["Resource"]["Fn::ImportValue"]
    (lifecycle,) = with_service(statements, "autoscaling")
    assert sorted(as_list(lifecycle["Action"])) == [
        "autoscaling:CompleteLifecycleAction",
        "autoscaling:RecordLifecycleActionHeartbeat",
    ]
    assert lifecycle["Condition"] == {"StringEquals": {"autoscaling:ResourceTag/rhi-pool": "mock"}}


def test_on_every_resource_only_the_five_session_actions(statements: list[dict[str, Any]]) -> None:
    on_every_resource = {
        action
        for statement in statements
        if "*" in as_list(statement["Resource"])
        for action in as_list(statement["Action"])
    }
    assert on_every_resource == SESSION_ACTIONS
    # And no other statement than those above.
    services = {action.split(":")[0] for statement in statements for action in as_list(statement["Action"])}
    assert services == {"dynamodb", "logs", "aps", "autoscaling", "ssm", "ssmmessages"}
