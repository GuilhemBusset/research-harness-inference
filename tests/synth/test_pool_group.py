"""A pool's launch template, Auto Scaling group, warm pool and nightly stop (system design, sections
4.2, 4.3, 5.1 and 5.4): the mock pool of the example configuration, a pool with a fallback instance
type, and a pool with no warm instance.

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
    from collections.abc import Mapping
    from types import ModuleType

    import aws_cdk as cdk

    from rhi_common.pool_config import Pool

EXAMPLE = config.load({"RHI_CONFIG": "example"})
(MOCK,) = EXAMPLE.pools
IMAGE_PARAMETER = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"


def of_type(template: Mapping[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template["Resources"].items() if r["Type"] == kind}


@pytest.fixture(scope="module")
def pools(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stage = app.node.find_child("Rhi")
    stack = stage.pools  # ty: ignore[unresolved-attribute] - the app's stage, typed as a construct
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[unresolved-attribute]
    return templates[path]


@pytest.fixture(scope="module")
def group(pools: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    (found,) = of_type(pools, "AWS::AutoScaling::AutoScalingGroup").items()
    return found


def test_the_mock_pool_launch_template(pools: dict[str, Any]) -> None:
    (template,) = of_type(pools, "AWS::EC2::LaunchTemplate").values()
    data = template["LaunchTemplateData"]
    assert data["MetadataOptions"] == {"HttpTokens": "required", "InstanceMetadataTags": "enabled"}
    assert "KeyName" not in data
    (interface,) = data["NetworkInterfaces"]
    assert interface["AssociatePublicIpAddress"] is False
    (security_group,) = interface["Groups"]
    assert "SecurityGroupsInstances" in security_group["Fn::ImportValue"]
    (volume,) = data["BlockDeviceMappings"]
    assert volume["Ebs"]["Encrypted"] is True
    (instance,) = [spec for spec in data["TagSpecifications"] if spec["ResourceType"] == "instance"]
    tags = {tag["Key"]: tag["Value"] for tag in instance["Tags"]}
    assert tags["rhi-pool"] == "mock"
    assert tags["rhi-release-parameter"] == "/rhi/pools/mock/release"
    user_data = Path(__file__).resolve().parents[2] / "services" / "engine" / "boot" / "user-data.sh"
    assert data["UserData"]["Fn::Base64"] == user_data.read_text(encoding="utf-8")


def test_the_mock_image_is_read_from_the_public_parameter_at_deployment(pools: dict[str, Any]) -> None:
    (template,) = of_type(pools, "AWS::EC2::LaunchTemplate").values()
    parameter = template["LaunchTemplateData"]["ImageId"]["Ref"]
    assert pools["Parameters"][parameter] == {
        "Type": "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
        "Default": IMAGE_PARAMETER,
    }


def test_the_mock_pool_group(pools: dict[str, Any], group: tuple[str, dict[str, Any]]) -> None:
    _, properties = group
    assert properties["MinSize"] == "0"
    assert properties["MaxSize"] == str(MOCK.max_instances) == "2"
    assert "DesiredCapacity" not in properties
    assert properties["NewInstancesProtectedFromScaleIn"] is True
    assert [tag["Value"] for tag in properties["Tags"] if tag["Key"] == "rhi-pool"] == ["mock"]
    # The private subnets of the three zones, and the pool's target group.
    subnets = [subnet["Fn::ImportValue"] for subnet in properties["VPCZoneIdentifier"]]
    assert len(subnets) == 3
    assert all(f"PrivateSubnet{zone}" in subnet for zone, subnet in enumerate(subnets, start=1))
    (target_group,) = of_type(pools, "AWS::ElasticLoadBalancingV2::TargetGroup")
    assert properties["TargetGroupARNs"] == [{"Ref": target_group}]
    assert of_type(pools, "AWS::AutoScaling::ScalingPolicy") == {}


def test_rebalancing_is_suspended_when_the_group_is_created_or_replaced(
    pools: dict[str, Any], group: tuple[str, dict[str, Any]]
) -> None:
    name, _ = group
    (suspend,) = of_type(pools, "Custom::AWS").values()
    for event in ("Create", "Update"):
        parts = suspend[event]["Fn::Join"][1]
        call = json.loads("".join(part if isinstance(part, str) else "NAME" for part in parts))
        assert {"Ref": name} in parts
        assert call["service"] == "AutoScaling"
        assert call["action"] == "suspendProcesses"
        assert call["parameters"] == {"AutoScalingGroupName": "NAME", "ScalingProcesses": ["AZRebalance"]}


def test_the_mock_pool_warm_pool(pools: dict[str, Any], group: tuple[str, dict[str, Any]]) -> None:
    name, _ = group
    (warm_pool,) = of_type(pools, "AWS::AutoScaling::WarmPool").values()
    assert warm_pool["AutoScalingGroupName"] == {"Ref": name}
    assert warm_pool["PoolState"] == "Stopped"
    assert warm_pool["MaxGroupPreparedCapacity"] == MOCK.warm_instances == 1
    assert warm_pool["MinSize"] == 0
    assert warm_pool["InstanceReusePolicy"] == {"ReuseOnScaleIn": True}


def test_the_mock_pool_nightly_stop(pools: dict[str, Any], group: tuple[str, dict[str, Any]]) -> None:
    name, _ = group
    (action,) = of_type(pools, "AWS::AutoScaling::ScheduledAction").values()
    assert action["AutoScalingGroupName"] == {"Ref": name}
    assert EXAMPLE.nightly_stop == "02:00"
    assert action["Recurrence"] == "0 2 * * *"
    assert action["TimeZone"] == EXAMPLE.time_zone
    assert action["DesiredCapacity"] == 0
    assert "MinSize" not in action
    assert "MaxSize" not in action


def standalone(app_module: ModuleType, pool: Pool, *, nightly_stop: str = "02:00") -> Mapping[str, Any]:
    """The template of a stack that holds only this pool's group, in a VPC of its own."""
    from aws_cdk import assertions
    from aws_cdk import aws_ec2 as ec2
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    from rhi.pools.group import PoolGroup

    cdk = app_module.cdk
    stack = cdk.Stack(cdk.App(analytics_reporting=False), "Group")
    vpc = ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=1)
    target_group = elbv2.ApplicationTargetGroup(stack, "Targets", vpc=vpc, port=8000, target_type=elbv2.TargetType.INSTANCE)
    PoolGroup(
        stack,
        "Pool",
        pool=pool,
        vpc=vpc,
        security_group=ec2.SecurityGroup(stack, "Instances", vpc=vpc),
        target_group=target_group,
        nightly_stop=nightly_stop,
        time_zone="Europe/Paris",
    )
    return assertions.Template.from_stack(stack).to_json()


def test_types_in_order(app_module: ModuleType) -> None:
    pool = dataclasses.replace(MOCK, name="two", instance_types=("t4g.medium", "t4g.small"))
    template = standalone(app_module, pool)
    (properties,) = of_type(template, "AWS::AutoScaling::AutoScalingGroup").values()
    policy = properties["MixedInstancesPolicy"]
    # In the configuration's order, with no WeightedCapacity, and On-Demand only.
    assert policy["LaunchTemplate"]["Overrides"] == [{"InstanceType": "t4g.medium"}, {"InstanceType": "t4g.small"}]
    assert policy["InstancesDistribution"] == {
        "OnDemandAllocationStrategy": "prioritized",
        "OnDemandBaseCapacity": 0,
        "OnDemandPercentageAboveBaseCapacity": 100,
    }


def test_no_warm_pool_at_zero(app_module: ModuleType) -> None:
    template = standalone(app_module, dataclasses.replace(MOCK, warm_instances=0), nightly_stop="23:05")
    assert len(of_type(template, "AWS::AutoScaling::AutoScalingGroup")) == 1
    assert of_type(template, "AWS::AutoScaling::WarmPool") == {}
    # The nightly stop does not depend on a warm pool, and follows the configured time.
    (action,) = of_type(template, "AWS::AutoScaling::ScheduledAction").values()
    assert (action["Recurrence"], action["TimeZone"], action["DesiredCapacity"]) == ("5 23 * * *", "Europe/Paris", 0)


def test_every_configured_pool_synthesizes_with_its_findings_acknowledged(
    app_module: ModuleType, tmp_path: Path
) -> None:
    """A pool of any name, and a GPU pool, each get a group: the app passes cdk-nag, the GPU image
    is read at deployment from the parameter the promotion job writes, and each custom resource
    suspends processes only of its own pool's group."""
    gpu = dataclasses.replace(
        MOCK,
        name="coursework",
        engine="vllm",
        model="example/model",
        revision="a" * 40,
        instance_types=("g7e.2xlarge",),
        prices={"g7e.2xlarge": 3.36},
    )
    pools = (dataclasses.replace(MOCK, name="second"), gpu)
    configured = dataclasses.replace(
        EXAMPLE,
        pools=pools,
        default_pool=gpu.name,
        allowed_instance_types=tuple(t for pool in pools for t in pool.instance_types),
    )
    cdk_json = Path(__file__).resolve().parents[2] / "cdk.json"
    context = json.loads(cdk_json.read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(configured, outdir=str(tmp_path), context=context)
    app.synth()  # Fails on a finding no file acknowledges.
    stage = app.node.find_child("Rhi")
    template = json.loads((Path(stage.outdir) / stage.pools.template_file).read_text(encoding="utf-8"))
    groups = of_type(template, "AWS::AutoScaling::AutoScalingGroup").values()
    assert sorted(tag["Value"] for group in groups for tag in group["Tags"] if tag["Key"] == "rhi-pool") == [
        "coursework",
        "second",
    ]
    images = [
        template["Parameters"][launch["LaunchTemplateData"]["ImageId"]["Ref"]]
        for launch in of_type(template, "AWS::EC2::LaunchTemplate").values()
    ]
    assert sorted(image["Default"] for image in images) == sorted([IMAGE_PARAMETER, "/rhi/images/gpu"])
    conditions = [
        statement["Condition"]
        for policy in of_type(template, "AWS::IAM::Policy").values()
        for statement in policy["PolicyDocument"]["Statement"]
        if statement["Action"] == "autoscaling:SuspendProcesses"
    ]
    assert sorted(c["StringEquals"]["autoscaling:ResourceTag/rhi-pool"] for c in conditions) == ["coursework", "second"]
