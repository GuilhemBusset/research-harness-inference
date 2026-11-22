"""A pool's Auto Scaling group, its launch template, its warm pool and its nightly stop (system design,
sections 4.2, 4.3, 5.1 and 5.4).

A pool becomes one group, whose instances register with the pool's target group (`rhi.pools.routing`).
The group holds to the fleet cap (`tests/policy/test_fleet_cap.py`): a minimum of zero, the pool's
`max_instances` as its maximum, no desired capacity in the template and no scaling policy. The
scaler is the one writer of the desired capacity, and the group protects every new instance from
scale-in, so that only the scaler, which lifts the protection, chooses which instance leaves.

The group spans the private subnets of the VPC's three zones, and launches the pool's instance types
in the order the configuration lists them: the first, then each fallback when a zone has none of
the one before. They are On-Demand only and carry no weights, which a warm pool requires of a group
with several types (evidence E10).

A pool whose `warm_instances` is above zero has a warm pool of stopped instances, which cost only
their disk and keep the pulled image: its prepared capacity, the instances running or stopped that
the group keeps, is `warm_instances`, and its minimum is zero, since a minimum above zero would
launch a fresh instance to refill the pool on every wake (section 4.2; evidence E10). Instances
return to it on scale-in. A pool whose `warm_instances` is zero has no warm pool.

What stays outside the scaler, so that a broken scaler cannot cost money, includes a scheduled
action that sets the group's desired capacity to zero every night, at the configuration's
`nightly_stop` in its `time_zone` (sections 5.1 and 17). It sets neither the minimum nor the
maximum, which the fleet cap checks, and leaves the warm pool as it is.

The group publishes its group metrics to CloudWatch every minute, the warm pool's among them,
whether or not the pool has a warm pool: the alarms that guard money from outside the scaler read
them (`rhi.guardrails.alarms_fleet`; sections 5.1 and 7.3). Auto Scaling publishes them at no
charge.

Zone rebalancing is suspended, because it replaces instances in service (section 4.3). No property
of the group suspends a process for good, so a custom resource calls `SuspendProcesses` for
`AZRebalance` when the group is created, and again when it is replaced: a replaced group has a new
name, which changes the custom resource's call, and CloudFormation then updates it. The scaler
resumes only the process `Launch`, so rebalancing stays suspended.

The launch template gives an instance the pool's role (`rhi.pools.role`), no public address and no
key pair, requires IMDSv2, lets the instance read its own tags through the instance metadata and encrypts its volume. It tags the
instance with its pool, `rhi-pool`, and with the name of its pool's release parameter,
`rhi-release-parameter`: the boot program installed by the user data
(`services/engine/boot/user-data.sh`) reads that tag to find its release, so the user data holds no
setting.

A GPU pool's instance gets a volume of 150 GB: the engine's image, Docker's data and the engine's
compile cache live on it (section 4.3), and a stopped warm instance costs only its disk, about $12 a
month at that size (section 5.2). The mock pool's volume keeps the size of its image.

The group and the launch template themselves carry the pool's name under the tag `rhi-pool` and its
engine under the tag `rhi-engine`. The pipeline's jobs that work on a pool (the promotion of a
machine image, the GPU verification) cannot refer to a resource of the stage, so they find a pool's
group by asking Auto Scaling for these tags.

The mock pool's machine image is the ECS-optimized Amazon Linux 2023 image for arm64, which carries
Docker: its ID is read from the parameter AWS publishes for it when the stack is deployed, and
nothing is looked up at synthesis. A GPU pool, a pool whose engine is not mock, takes its image
from its own parameter of the owner's account (`rhi.pools.image`): it too is read when the stack is
deployed, so a merge never changes the image.

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

from rhi.pools import image

if TYPE_CHECKING:
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2

    from rhi_common.pool_config import Pool

# The user data of every pool's launch template (issue E2-38), carried whole.
USER_DATA = Path(__file__).resolve().parents[3] / "services" / "engine" / "boot" / "user-data.sh"

# The instance tags the boot program reads, and the parameter the second names.
POOL_TAG = "rhi-pool"
RELEASE_PARAMETER_TAG = "rhi-release-parameter"

# The tag that holds the pool's engine, beside `POOL_TAG`, on the group and on the launch template.
ENGINE_TAG = "rhi-engine"


def release_parameter(pool: str) -> str:
    """The name of a pool's release parameter, which the pipeline writes (issue E2-42)."""
    return f"/rhi/pools/{pool}/release"


# The public parameter AWS publishes for the current ECS-optimized Amazon Linux 2023 image for arm64,
# and the root device of that image.
MOCK_IMAGE_PARAMETER = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"
MOCK_ROOT_DEVICE = "/dev/xvda"

# The process that replaces instances in service to even out the zones.
REBALANCING = "AZRebalance"

# The size, in GiB, of a GPU pool's volume (sections 4.3 and 5.2).
GPU_VOLUME_GIB = 150

# The group metrics the group publishes, every minute: the group's and its warm pool's, every one
# Auto Scaling has.
METRICS_GRANULARITY = "1Minute"
GROUP_METRICS = (
    "GroupMinSize",
    "GroupMaxSize",
    "GroupDesiredCapacity",
    "GroupInServiceInstances",
    "GroupPendingInstances",
    "GroupStandbyInstances",
    "GroupTerminatingInstances",
    "GroupTotalInstances",
    "GroupInServiceCapacity",
    "GroupPendingCapacity",
    "GroupStandbyCapacity",
    "GroupTerminatingCapacity",
    "GroupTotalCapacity",
    "WarmPoolDesiredCapacity",
    "WarmPoolWarmedCapacity",
    "WarmPoolPendingCapacity",
    "WarmPoolTerminatingCapacity",
    "WarmPoolTotalCapacity",
    "GroupAndWarmPoolDesiredCapacity",
    "GroupAndWarmPoolTotalCapacity",
)


def machine_image(pool: Pool) -> tuple[ec2.IMachineImage, str]:
    """The pool's machine image, resolved when the stack is deployed, and the image's root device."""
    if image.is_gpu_pool(pool):
        return image.gpu_image(pool.name), image.ROOT_DEVICE
    return ec2.MachineImage.from_ssm_parameter(MOCK_IMAGE_PARAMETER), MOCK_ROOT_DEVICE


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
        role: iam.Role,
        nightly_stop: str,
        time_zone: str,
    ) -> None:
        super().__init__(scope, construct_id)
        pool_image, root_device = machine_image(pool)
        self.launch_template = ec2.LaunchTemplate(
            self,
            "LaunchTemplate",
            machine_image=pool_image,
            user_data=ec2.UserData.custom(USER_DATA.read_text(encoding="utf-8")),
            security_group=security_group,
            # Through an instance profile the launch template makes for it.
            role=role,  # ty: ignore[invalid-argument-type] - jsii passes `grantee` by position (rhi.pools.role)
            associate_public_ip_address=False,
            require_imdsv2=True,
            http_tokens=ec2.LaunchTemplateHttpTokens.REQUIRED,
            instance_metadata_tags=True,
            # The image's root volume, encrypted with the account's EBS key: a GPU pool's at 150 GB,
            # the mock pool's at the image's size.
            block_devices=[
                ec2.BlockDevice(
                    device_name=root_device,
                    volume=ec2.BlockDeviceVolume(
                        ebs_device=ec2.EbsDeviceProps(
                            encrypted=True,
                            volume_type=ec2.EbsDeviceVolumeType.GP3,
                            volume_size=GPU_VOLUME_GIB if image.is_gpu_pool(pool) else None,
                        )
                    ),
                )
            ],
        )
        # On the template, and through its tag specifications on each instance and its volume.
        cdk.Tags.of(self.launch_template).add(POOL_TAG, pool.name)
        cdk.Tags.of(self.launch_template).add(ENGINE_TAG, pool.engine)
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
            metrics_collection=[
                autoscaling.CfnAutoScalingGroup.MetricsCollectionProperty(
                    granularity=METRICS_GRANULARITY, metrics=list(GROUP_METRICS)
                )
            ],
            # The template's tags reach the instances; the group's tags say which pool it is, and
            # which engine it runs.
            tags=[
                autoscaling.CfnAutoScalingGroup.TagPropertyProperty(key=key, value=value, propagate_at_launch=False)
                for key, value in ((POOL_TAG, pool.name), (ENGINE_TAG, pool.engine))
            ],
        )

        if pool.warm_instances > 0:
            self.warm_pool: autoscaling.CfnWarmPool | None = autoscaling.CfnWarmPool(
                self,
                "WarmPool",
                auto_scaling_group_name=self.group.ref,
                pool_state="Stopped",
                max_group_prepared_capacity=pool.warm_instances,
                min_size=0,
                instance_reuse_policy=autoscaling.CfnWarmPool.InstanceReusePolicyProperty(reuse_on_scale_in=True),
            )
        else:
            self.warm_pool = None

        # The desired capacity alone: "HH:MM" becomes the cron "MM HH * * *" in the time zone.
        hours, minutes = nightly_stop.split(":")
        self.nightly_stop = autoscaling.CfnScheduledAction(
            self,
            "NightlyStop",
            auto_scaling_group_name=self.group.ref,
            desired_capacity=0,
            recurrence=f"{int(minutes)} {int(hours)} * * *",
            time_zone=time_zone,
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
