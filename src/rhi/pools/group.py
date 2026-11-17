"""A pool's Auto Scaling group and its launch template (system design, sections 4.2, 4.3 and 5.4).

A pool becomes one group, whose instances register with the pool's target group (`rhi.pools.routing`).
The group holds to the fleet cap (`tests/policy/test_fleet_cap.py`): a minimum of zero, the pool's
`max_instances` as its maximum, no desired capacity in the template and no scaling policy. The
scaler is the one writer of the desired capacity, and the group protects every new instance from
scale-in, so that only the scaler, which lifts the protection, chooses which instance leaves.

The group spans the private subnets of the VPC's three zones, and launches the pool's instance types
in the order the configuration lists them: the first, then each fallback when a zone has none of
the one before. They are On-Demand only and carry no weights, which a warm pool requires of a group
with several types (evidence E10).

Zone rebalancing is suspended, because it replaces instances in service (section 4.3). No property
of the group suspends a process for good, so a custom resource calls `SuspendProcesses` for
`AZRebalance` when the group is created, and again when it is replaced: a replaced group has a new
name, which changes the custom resource's call, and CloudFormation then updates it. The scaler
resumes only the process `Launch`, so rebalancing stays suspended.

The launch template gives an instance no public address and no key pair, requires IMDSv2, lets the
instance read its own tags through the instance metadata and encrypts its volume. It tags the
instance with its pool, `rhi-pool`, and with the name of its pool's release parameter,
`rhi-release-parameter`: the boot program installed by the user data
(`services/engine/boot/user-data.sh`) reads that tag to find its release, so the user data holds no
setting.

The mock pool's machine image is the ECS-optimized Amazon Linux 2023 image for arm64, which carries
Docker: its ID is read from the parameter AWS publishes for it when the stack is deployed, and
nothing is looked up at synthesis. A GPU pool's image is AWS's base GPU image on Ubuntu, whose ID
the promotion job writes to a parameter of the owner's account, `/rhi/images/gpu`, after a canary
start on it passes (section 4.3): it too is read when the stack is deployed, so a merge never
changes the image, and a stage with a GPU pool deploys once that parameter exists.

The custom resource may suspend processes only of a group tagged with its pool: Auto Scaling puts an
identifier it chooses at creation before the group's name in the group's ARN, so the ARN is named by
pattern, and the tag the group carries from its creation narrows it to the pool's own group.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import custom_resources as cr
from constructs import Construct

if TYPE_CHECKING:
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    from rhi_common.pool_config import Pool

# The user data of every pool's launch template (issue E2-38), carried whole.
USER_DATA = Path(__file__).resolve().parents[3] / "services" / "engine" / "boot" / "user-data.sh"

# The instance tags the boot program reads, and the parameter the second names.
POOL_TAG = "rhi-pool"
RELEASE_PARAMETER_TAG = "rhi-release-parameter"


def release_parameter(pool: str) -> str:
    """The name of a pool's release parameter, which the pipeline writes (issue E2-42)."""
    return f"/rhi/pools/{pool}/release"


# The public parameter AWS publishes for the current ECS-optimized Amazon Linux 2023 image for arm64,
# and the root device of that image.
MOCK_IMAGE_PARAMETER = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"
MOCK_ROOT_DEVICE = "/dev/xvda"

# The parameter of the owner's account that the promotion job writes with the GPU image's ID, and
# the root device of that image (an Ubuntu image).
GPU_IMAGE_PARAMETER = "/rhi/images/gpu"
GPU_ROOT_DEVICE = "/dev/sda1"

# The process that replaces instances in service to even out the zones.
REBALANCING = "AZRebalance"


def machine_image(pool: Pool) -> tuple[ec2.IMachineImage, str]:
    """The pool's machine image, resolved when the stack is deployed, and the image's root device."""
    if pool.engine == "mock":
        return ec2.MachineImage.from_ssm_parameter(MOCK_IMAGE_PARAMETER), MOCK_ROOT_DEVICE
    return ec2.MachineImage.from_ssm_parameter(GPU_IMAGE_PARAMETER), GPU_ROOT_DEVICE


class PoolGroup(Construct):
    """One pool's launch template, as `launch_template`, and Auto Scaling group, as `group`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        pool: Pool,
        vpc: ec2.IVpc,
        security_group: ec2.ISecurityGroup,
        target_group: elbv2.IApplicationTargetGroup,
    ) -> None:
        super().__init__(scope, construct_id)
        image, root_device = machine_image(pool)
        self.launch_template = ec2.LaunchTemplate(
            self,
            "LaunchTemplate",
            machine_image=image,
            user_data=ec2.UserData.custom(USER_DATA.read_text(encoding="utf-8")),
            security_group=security_group,
            associate_public_ip_address=False,
            require_imdsv2=True,
            http_tokens=ec2.LaunchTemplateHttpTokens.REQUIRED,
            instance_metadata_tags=True,
            # The image's root volume, at the image's size, encrypted with the account's EBS key.
            block_devices=[
                ec2.BlockDevice(
                    device_name=root_device,
                    volume=ec2.BlockDeviceVolume(
                        ebs_device=ec2.EbsDeviceProps(encrypted=True, volume_type=ec2.EbsDeviceVolumeType.GP3)
                    ),
                )
            ],
        )
        # On the template, and through its tag specifications on each instance and its volume.
        cdk.Tags.of(self.launch_template).add(POOL_TAG, pool.name)
        cdk.Tags.of(self.launch_template).add(RELEASE_PARAMETER_TAG, release_parameter(pool.name))

        self.group = autoscaling.CfnAutoScalingGroup(
            self,
            "Group",
            min_size="0",
            max_size=str(pool.max_instances),
            vpc_zone_identifier=vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnet_ids,
            new_instances_protected_from_scale_in=True,
            target_group_arns=[target_group.target_group_arn],
            mixed_instances_policy=autoscaling.CfnAutoScalingGroup.MixedInstancesPolicyProperty(
                launch_template=autoscaling.CfnAutoScalingGroup.LaunchTemplateProperty(
                    launch_template_specification=autoscaling.CfnAutoScalingGroup.LaunchTemplateSpecificationProperty(
                        launch_template_id=self.launch_template.launch_template_id,
                        version=self.launch_template.latest_version_number,
                    ),
                    overrides=[
                        autoscaling.CfnAutoScalingGroup.LaunchTemplateOverridesProperty(instance_type=instance_type)
                        for instance_type in pool.instance_types
                    ],
                ),
                # On-Demand only, each type in the order of the overrides.
                instances_distribution=autoscaling.CfnAutoScalingGroup.InstancesDistributionProperty(
                    on_demand_allocation_strategy="prioritized",
                    on_demand_base_capacity=0,
                    on_demand_percentage_above_base_capacity=100,
                ),
            ),
            # The template's tags reach the instances; the group's tag says which pool it is.
            tags=[
                autoscaling.CfnAutoScalingGroup.TagPropertyProperty(
                    key=POOL_TAG, value=pool.name, propagate_at_launch=False
                )
            ],
        )

        stack = cdk.Stack.of(self)
        # Every group, by the pattern of its ARN, narrowed to the pool's by the group's tag.
        groups = stack.format_arn(
            service="autoscaling",
            resource="autoScalingGroup",
            resource_name="*:autoScalingGroupName/*",
            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
        )
        call = cr.AwsSdkCall(
            service="AutoScaling",
            action="suspendProcesses",
            parameters={"AutoScalingGroupName": self.group.ref, "ScalingProcesses": [REBALANCING]},
            physical_resource_id=cr.PhysicalResourceId.of(self.group.ref),
        )
        self.rebalancing_suspended = cr.AwsCustomResource(
            self,
            "SuspendRebalancing",
            on_create=call,
            on_update=call,
            install_latest_aws_sdk=False,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["autoscaling:SuspendProcesses"],
                        resources=[groups],
                        conditions={"StringEquals": {f"autoscaling:ResourceTag/{POOL_TAG}": pool.name}},
                    )
                ]
            ),
        )
