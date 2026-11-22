"""The access logs of both load balancers, in one bucket that keeps them 90 days (system design,
sections 6.6 and 10).

A load balancer's access log holds each client's source address, which is personal data: kept no
longer than course end plus 90 days. Each object of the bucket expires 90 days after it is written,
which makes that hold, as the log groups' retention does (`rhi.retention`). The bucket is stateful,
and holds personal data: it is retained on deletion and on replacement, and its logical ID is
recorded in `tests/policy/stateful_logical_ids.json`.

**Who writes.** Elastic Load Balancing writes only to a bucket encrypted with S3-managed keys. The
bucket's policy lets its log-delivery service, `LOG_DELIVERY_SERVICE`, put objects under each load
balancer's prefix, below `AWSLogs/<account>/`, where the service writes them. The service is the
policy's only principal, in its only statement: the older form of that policy names an AWS-owned account per region, which
an environment-agnostic stack cannot know and a public repository should not hold. The only account
the policy names is the stack's own, a deployment-time value.

**Each load balancer under a prefix of its own**: `PUBLIC_PREFIX` for the public one
(`rhi.network.edge`), `INTERNAL_PREFIX` for the internal one (`rhi.pools.routing`). Each sets its
access-log attributes itself, with `access_log_attributes`: the CDK's helper for them may refuse a
stack with no fixed region. That drops the ordering the helper adds, and Elastic Load Balancing
refuses the attributes unless the policy already lets it write: so each load balancer depends on
`policy`, the public one directly, the internal one through its stack's dependency on the network
stack.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

# Section 6.6: course end plus 90 days, as a period from when each object is written.
EXPIRY_DAYS = 90
# The service Elastic Load Balancing delivers access logs as.
LOG_DELIVERY_SERVICE = "logdelivery.elasticloadbalancing.amazonaws.com"
# The prefix of each load balancer's logs.
PUBLIC_PREFIX = "public"
INTERNAL_PREFIX = "internal"
PREFIXES = (PUBLIC_PREFIX, INTERNAL_PREFIX)


def access_log_attributes(bucket: s3.IBucket, prefix: str) -> dict[str, str]:
    """The attributes that have a load balancer write its access logs to the bucket under the
    prefix."""
    return {
        "access_logs.s3.enabled": "true",
        "access_logs.s3.bucket": bucket.bucket_name,
        "access_logs.s3.prefix": prefix,
    }


class AccessLogs(Construct):
    """The bucket of the load balancers' access logs, as `bucket`, and its policy, as `policy`,
    which a load balancer that logs to it depends on."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        self.bucket = s3.Bucket(
            self,
            "Bucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # The only encryption Elastic Load Balancing writes access logs under.
            encryption=s3.BucketEncryption.S3_MANAGED,
            # A bucket with no ACL: its policy says who writes.
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            # No refusal of requests without TLS: it is a statement on every principal, and the
            # log-delivery service is the policy's only principal (AwsSolutions-S10, acknowledged).
            enforce_ssl=False,
            lifecycle_rules=[s3.LifecycleRule(id="Expire", expiration=cdk.Duration.days(EXPIRY_DAYS))],
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="LoadBalancerAccessLogs",
                # ty reads the CDK's generated classes as unlike the protocols they implement where
                # a method names a parameter otherwise: jsii passes it by position.
                principals=[iam.ServicePrincipal(LOG_DELIVERY_SERVICE)],  # ty: ignore[invalid-argument-type]
                actions=["s3:PutObject"],
                resources=[self.bucket.arn_for_objects(f"{prefix}/AWSLogs/{stack.account}/*") for prefix in PREFIXES],
            )
        )
        assert self.bucket.policy is not None
        policy = self.bucket.policy.node.default_child
        assert isinstance(policy, s3.CfnBucketPolicy)
        self.policy: s3.CfnBucketPolicy = policy
