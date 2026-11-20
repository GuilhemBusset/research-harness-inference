"""The model bucket: the pools' weights at rest (system design, section 4.4).

Weights live in S3: cheapest at rest, and streaming from S3 is within seconds of local disk
(evidence E11). The bucket is stateful: it is retained on deletion and on replacement, and its
logical ID is recorded in `tests/policy/stateful_logical_ids.json`.

**Its name is fixed.** The one writer of the bucket is the copy job's role, which lives in the
pipeline stack, and the pipeline stack cannot refer to a resource of the stage. So the name is
what `bucket_name` builds from `NAME_PREFIX`, the account and the region, and the stage and the
pipeline stack both call it with their own account and region, deployment-time values. The
repository holds the prefix only. A policy names the bucket by `bucket_arn` and its objects by
`objects_arn`, written with that name: a statement an invariant can read, in any stack.

**Layout.** A pool's weights are under `<pool>/<revision>/` (`pool_prefix`), so that a new
revision is written beside the one in service and an instance never loads a half-replaced model.
Each GPU pool's instance role reads its own pool's prefix and nothing else (`rhi.pools.role`).

**Reads go through the endpoint; writes cannot.** Instances read weights through the VPC's S3
gateway endpoint. The bucket's policy refuses reading an object by any request that does not come
through that endpoint: a request from anywhere else carries another endpoint's ID or none, and
either way the condition holds and the read is refused, whatever the caller's own policy allows.
The copy job runs in CodeBuild, outside the VPC, so the policy says nothing of where a write or
a listing comes from: the copy job never reads an object, and learns what a prefix holds by
listing it.

The bucket's policy allows nothing: who may write and who may read is said by each role's own
policy, the copy job's in the pipeline stack and each GPU pool's in `rhi.pools.role`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

if TYPE_CHECKING:
    from aws_cdk import aws_ec2 as ec2

# What the bucket's name starts with. The account and the region follow, at deployment.
NAME_PREFIX = "rhi-models"
# What reading an object is to S3: its content, at its current version or at a named one. Reading
# an object's attributes or its head needs one of the two as well.
OBJECT_READ_ACTIONS = ("s3:GetObject", "s3:GetObjectVersion")
# The condition key that holds the ID of the VPC endpoint a request came through, when it came
# through one.
SOURCE_ENDPOINT = "aws:SourceVpce"


def bucket_name(account: str, region: str) -> str:
    """The model bucket's name in an account and a region. Both may be deployment-time values, as
    `Stack.account` and `Stack.region` are in an environment-agnostic stack."""
    return f"{NAME_PREFIX}-{account}-{region}"


def bucket_arn(stack: cdk.Stack) -> str:
    """The ARN of the model bucket of the stack's account and region, written with its name."""
    return stack.format_arn(service="s3", region="", account="", resource=bucket_name(stack.account, stack.region))


def pool_prefix(pool: str) -> str:
    """The prefix of a pool's weights, every revision of them."""
    return f"{pool}/"


def objects_arn(stack: cdk.Stack, pool: str) -> str:
    """The ARN pattern of every object under a pool's prefix."""
    return f"{bucket_arn(stack)}/{pool_prefix(pool)}*"


class ModelStore(Construct):
    """The model bucket, as `bucket`: private, encrypted, retained, and read only through
    `endpoint`, the VPC's S3 gateway endpoint."""

    def __init__(self, scope: Construct, construct_id: str, *, endpoint: ec2.IGatewayVpcEndpoint) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        self.bucket = s3.Bucket(
            self,
            "Bucket",
            bucket_name=bucket_name(stack.account, stack.region),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            # A bucket with no ACL: its policy and its readers' policies say who reads.
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="ReadObjectsThroughTheEndpointOnly",
                effect=iam.Effect.DENY,
                # ty reads the CDK's generated classes as unlike the protocols they implement where
                # a method names a parameter otherwise: jsii passes it by position.
                principals=[iam.AnyPrincipal()],  # ty: ignore[invalid-argument-type]
                actions=list(OBJECT_READ_ACTIONS),
                resources=[self.bucket.arn_for_objects("*")],
                # A request that came through no endpoint has no such key, and `StringNotEquals`
                # holds for a key that is absent: it is refused like one through another endpoint.
                conditions={"StringNotEquals": {SOURCE_ENDPOINT: endpoint.vpc_endpoint_id}},
            )
        )
