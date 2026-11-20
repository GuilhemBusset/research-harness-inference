"""A GPU pool's machine image (system design, section 4.3).

A GPU pool is a pool whose engine is not mock. Its image is AWS's base GPU image with the
open-source NVIDIA driver on Ubuntu 22.04 (evidence E10), and the launch template names it by ID.
The ID is in no file of the repository: it is the value of a parameter of the owner's account, one
per pool, named `IMAGE_PARAMETER_PREFIX` followed by the pool's name. The owner writes a pool's
first value by hand before the pool enters the configuration, and the promotion job writes every
later one, only after a canary start on the new image passes. So a merge never changes the driver
under a pinned engine.

The launch template reads the parameter through a CloudFormation parameter, which CloudFormation
resolves when the stack is deployed: nothing is looked up at synthesis, and a stage with a GPU pool
deploys once that pool's parameter exists. The parameter is never one AWS publishes: CloudFormation
reads a parameter again on every stack update, so AWS's public path would bring the latest image
with any deployment (evidence E20).

The prefix lies outside the configuration's path in SSM, `rhi.config.SSM_PREFIX`: the configuration
loader reads every parameter under its own path as a key and refuses one it does not know, and the
image is state the promotion job writes, not configuration the owner edits.

This module creates no resource and no role: it brings no cdk-nag finding and no action.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_cdk import aws_ec2 as ec2

if TYPE_CHECKING:
    from rhi_common.pool_config import Pool

# What every pool's image parameter is named under, in the owner's account.
IMAGE_PARAMETER_PREFIX = "/rhi/images/"

# The root device of the image (an Ubuntu image).
ROOT_DEVICE = "/dev/sda1"

# The engine of a pool that needs no GPU.
MOCK_ENGINE = "mock"


def is_gpu_pool(pool: Pool) -> bool:
    """Whether the pool's instances run on the GPU image: every pool but a mock one."""
    return pool.engine != MOCK_ENGINE


def image_parameter(pool: str) -> str:
    """The name of the parameter that holds the ID of a GPU pool's machine image."""
    return f"{IMAGE_PARAMETER_PREFIX}{pool}"


def gpu_image(pool: str) -> ec2.IMachineImage:
    """A GPU pool's machine image, whose ID CloudFormation reads from the pool's parameter when
    the stack is deployed."""
    return ec2.MachineImage.from_ssm_parameter(image_parameter(pool))
