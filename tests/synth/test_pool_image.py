"""Where each pool's machine image comes from, and the tags that name a pool's group and launch
template (system design, section 4.3): a configuration with a mock pool and two GPU pools.

The CDK is imported after `app_module` has started jsii (see conftest.py), so `rhi.pools.image` is
imported inside the fixture.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

EXAMPLE = config.load({"RHI_CONFIG": "example"})
(EXAMPLE_MOCK,) = EXAMPLE.pools
MOCK = dataclasses.replace(EXAMPLE_MOCK, name="small")
GPU_POOLS = tuple(
    dataclasses.replace(
        EXAMPLE_MOCK,
        name=name,
        engine="vllm",
        model="example/model",
        revision="a" * 40,
        instance_types=("g7e.2xlarge",),
        prices={"g7e.2xlarge": 3.36},
    )
    for name in ("coursework", "second")
)
POOLS = (MOCK, *GPU_POOLS)
CONFIGURED = dataclasses.replace(
    EXAMPLE,
    pools=POOLS,
    default_pool=MOCK.name,
    allowed_instance_types=tuple(dict.fromkeys(t for pool in POOLS for t in pool.instance_types)),
)

PARAMETER_TYPE = "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>"
PUBLIC_MOCK_IMAGE = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"
IMAGE_ID = re.compile(r"\bami-[0-9a-f]{8,}")
REPO = Path(__file__).resolve().parents[2]


@dataclasses.dataclass(frozen=True)
class Synthesized:
    image: ModuleType
    text: str
    template: dict[str, Any]

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [r["Properties"] for r in self.template["Resources"].values() if r["Type"] == kind]

    def launch_template(self, pool: str) -> dict[str, Any]:
        """The launch template that tags its instances with this pool."""
        (found,) = [
            properties
            for properties in self.of_type("AWS::EC2::LaunchTemplate")
            for spec in properties["LaunchTemplateData"]["TagSpecifications"]
            if spec["ResourceType"] == "instance" and {"Key": "rhi-pool", "Value": pool} in spec["Tags"]
        ]
        return found

    def image_parameter(self, pool: str) -> dict[str, Any]:
        """The template's parameter that the pool's launch template takes its image ID from."""
        return self.template["Parameters"][self.launch_template(pool)["LaunchTemplateData"]["ImageId"]["Ref"]]


@pytest.fixture(scope="module")
def pools(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Synthesized:
    """The pools stack of the configuration above, synthesized as `cdk synth` would: synthesis
    fails on a cdk-nag finding that no file acknowledges."""
    from rhi.pools import image

    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app = app_module.build(CONFIGURED, outdir=str(tmp_path_factory.mktemp("cdk.out")), context=context)
    app.synth()
    stage = app.node.find_child("Rhi")
    text = (Path(stage.outdir) / stage.pools.template_file).read_text(encoding="utf-8")
    return Synthesized(image=image, text=text, template=json.loads(text))


@pytest.mark.parametrize("pool", [pool.name for pool in GPU_POOLS])
def test_a_gpu_pool_reads_its_image_at_deployment_from_its_own_parameter(pools: Synthesized, pool: str) -> None:
    name = pools.image.image_parameter(pool)
    assert pools.image_parameter(pool) == {"Type": PARAMETER_TYPE, "Default": name}
    # The module's prefix and the pool's name: outside the configuration's path, and not AWS's.
    assert name == pools.image.IMAGE_PARAMETER_PREFIX + pool
    assert not name.startswith(config.SSM_PREFIX)
    assert not config.SSM_PREFIX.startswith(pools.image.IMAGE_PARAMETER_PREFIX)
    assert not name.startswith("/aws/")


def test_only_the_mock_pool_reads_a_parameter_aws_publishes(pools: Synthesized) -> None:
    assert pools.image_parameter(MOCK.name) == {"Type": PARAMETER_TYPE, "Default": PUBLIC_MOCK_IMAGE}
    published = [
        name
        for name, parameter in pools.template["Parameters"].items()
        if str(parameter.get("Default", "")).startswith("/aws/")
    ]
    assert len(published) == 1


def test_no_image_id_is_written(pools: Synthesized) -> None:
    assert IMAGE_ID.search(pools.text) is None
    # Every image of the template is a reference to a parameter read at deployment.
    for launch in pools.of_type("AWS::EC2::LaunchTemplate"):
        assert set(launch["LaunchTemplateData"]["ImageId"]) == {"Ref"}
    for module in sorted((REPO / "src" / "rhi" / "pools").glob("*.py")):
        assert IMAGE_ID.search(module.read_text(encoding="utf-8")) is None, module.name


@pytest.mark.parametrize("pool", POOLS, ids=lambda pool: pool.name)
def test_the_group_and_the_launch_template_carry_their_pool_and_its_engine(pools: Synthesized, pool: Any) -> None:
    (group,) = [
        properties
        for properties in pools.of_type("AWS::AutoScaling::AutoScalingGroup")
        if {"Key": "rhi-pool", "Value": pool.name, "PropagateAtLaunch": False} in properties["Tags"]
    ]
    assert {"Key": "rhi-engine", "Value": pool.engine, "PropagateAtLaunch": False} in group["Tags"]
    assert [tag["Key"] for tag in group["Tags"]].count("rhi-engine") == 1
    # On the launch template itself, not only on the instances it launches.
    specifications = pools.launch_template(pool.name)["TagSpecifications"]
    (own,) = [spec for spec in specifications if spec["ResourceType"] == "launch-template"]
    tags = {tag["Key"]: tag["Value"] for tag in own["Tags"]}
    assert (tags["rhi-pool"], tags["rhi-engine"]) == (pool.name, pool.engine)
