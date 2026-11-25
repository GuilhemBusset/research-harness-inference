"""The coursework pool's group on the app with the example configuration (system design, sections
4.2, 4.3 and 4.5): its maximum, its one instance type, no warm pool whatever its entry gives and its
150 GB volume, and the mock pool's volume left at its image's size.

The CDK is imported inside the tests, after `app_module` has started jsii (see conftest.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import aws_cdk as cdk


def of_pool(template: dict[str, Any], kind: str, pool: str) -> dict[str, dict[str, Any]]:
    """The resources of a kind that a pool's constructs created: their logical IDs hold its name."""
    resources = template["Resources"].items()
    return {name: r["Properties"] for name, r in resources if r["Type"] == kind and pool in name}


@pytest.fixture(scope="module")
def pools(app: cdk.App, cloud_assembly: Path, templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stage = app.node.find_child("Rhi")
    stack = stage.pools  # ty: ignore[missing-attribute] - the app's stage, typed as a construct
    path = (Path(stage.outdir) / stack.template_file).relative_to(cloud_assembly).as_posix()  # ty: ignore[missing-attribute]
    return templates[path]


def volume(pools: dict[str, Any], pool: str) -> dict[str, Any]:
    (template,) = of_pool(pools, "AWS::EC2::LaunchTemplate", pool).values()
    (device,) = template["LaunchTemplateData"]["BlockDeviceMappings"]
    return device


def test_the_coursework_group(pools: dict[str, Any]) -> None:
    (group,) = of_pool(pools, "AWS::AutoScaling::AutoScalingGroup", "coursework").values()
    assert group["MinSize"] == "0"
    assert group["MaxSize"] == "2"
    assert "DesiredCapacity" not in group
    assert [tag["Value"] for tag in group["Tags"] if tag["Key"] == "rhi-pool"] == ["coursework"]
    overrides = group["MixedInstancesPolicy"]["LaunchTemplate"]["Overrides"]
    assert overrides == [{"InstanceType": "g7e.2xlarge"}]


def test_the_pools_stack_holds_no_warm_pool(pools: dict[str, Any]) -> None:
    assert [r for r in pools["Resources"].values() if r["Type"] == "AWS::AutoScaling::WarmPool"] == []


def test_the_coursework_volume_is_150_gb(pools: dict[str, Any]) -> None:
    device = volume(pools, "coursework")
    assert device["DeviceName"] == "/dev/sda1"
    assert device["Ebs"] == {"Encrypted": True, "VolumeType": "gp3", "VolumeSize": 150}


def test_the_mock_volume_keeps_its_image_size(pools: dict[str, Any]) -> None:
    device = volume(pools, "mock")
    assert device["DeviceName"] == "/dev/xvda"
    assert device["Ebs"] == {"Encrypted": True, "VolumeType": "gp3"}
