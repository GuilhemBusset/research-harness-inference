"""A pool's instance role and its instances' log group (system design, section 4.3): the mock pool
of the example configuration, and the IAM path of a GPU pool's role in a configuration that has one.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

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
    # And the registry's authorization token, which the pool's release grants with the pull of its
    # engine image (`rhi.pools.release`): AWS grants it on every resource only.
    assert on_every_resource == SESSION_ACTIONS | {"ecr:GetAuthorizationToken"}
    # And no other statement than those above and those of the pool's release, which reads its
    # parameter, its files' object and its engine image (`tests/synth/test_pool_release.py`).
    services = {action.split(":")[0] for statement in statements for action in as_list(statement["Action"])}
    assert services == {"dynamodb", "logs", "aps", "autoscaling", "secretsmanager", "ssm", "ssmmessages", "s3", "ecr"}


def test_a_gpu_pools_role_is_under_the_gpu_pools_path_and_the_mock_pools_is_not(
    app_module: ModuleType, tmp_path: Path
) -> None:
    from rhi.pools.role import GPU_ROLE_PATH

    example = config.load({"RHI_CONFIG": "example"})
    (mock,) = example.pools
    gpu = dataclasses.replace(
        mock,
        name="coursework",
        engine="vllm",
        model="example/model",
        revision="a" * 40,
        instance_types=("g7e.2xlarge",),
        prices={"g7e.2xlarge": 3.36},
    )
    configured = dataclasses.replace(
        example,
        pools=(mock, gpu),
        allowed_instance_types=(*mock.instance_types, *gpu.instance_types),
    )
    cdk_json = Path(__file__).resolve().parents[2] / "cdk.json"
    context = json.loads(cdk_json.read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(configured, outdir=str(tmp_path), context=context)
    app.synth()  # Fails on a finding of the roles that no file acknowledges.
    stage = app.node.find_child("Rhi")
    template = json.loads((Path(stage.outdir) / stage.pools.template_file).read_text(encoding="utf-8"))

    paths = {
        tag["Value"]: properties.get("Path", "/")
        for properties in of_type(template, "AWS::IAM::Role").values()
        for tag in properties.get("Tags", [])
        if tag["Key"] == "rhi-pool"
    }
    assert GPU_ROLE_PATH not in ("", "/")
    assert paths[gpu.name] == GPU_ROLE_PATH
    assert not paths[mock.name].startswith(GPU_ROLE_PATH)
    assert sorted(paths) == sorted([mock.name, gpu.name])
