"""The edge: the public load balancer, and its HTTPS listener on the owner's domain (system design,
sections 2, 5.3 and 6.5).

The load balancer is internet-facing, in the public subnets, behind the public load balancer's
security group. Its idle timeout is 1200 seconds, like the internal load balancer's: nothing else
in the path caps a long generation, and a held non-streaming request stays silent that long.

The listener takes TLS on 443 under the TLS 1.3 policy AWS recommends, named here and pinned by
`tests/policy/test_tls_policy.py`: the CDK's default still allows TLS 1.1 (evidence E26). Nothing
listens on port 80, since a redirect would mean the key already crossed in cleartext. A request
that no service's rule takes gets a fixed 404.

The certificate is validated by DNS in the configured hosted zone, which is imported by its ID and
never looked up. Until the domain resolves through that zone the validation cannot complete, so
the certificate, the listener and the alias record exist only when `https_listener` is true.

The load balancer writes its access logs to the stage's access-log bucket, under its own prefix
(`rhi.network.access_logs`). Its attributes are set here, not through the CDK's helper, and the
load balancer depends on the bucket's policy: Elastic Load Balancing refuses the attributes until
the policy lets it write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from constructs import Construct

from rhi.network.access_logs import PUBLIC_PREFIX, access_log_attributes
from rhi.network.security import HTTPS_PORT

if TYPE_CHECKING:
    from rhi.config import Config
    from rhi.network.access_logs import AccessLogs

IDLE_TIMEOUT_SECONDS = 1200
NOT_FOUND = 404


class Edge(Construct):
    """The public load balancer, as `load_balancer`, and its HTTPS listener, as `https_listener`,
    or None while the configuration leaves the listener off."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        security_group: ec2.ISecurityGroup,
        access_logs: AccessLogs,
        config: Config,
    ) -> None:
        super().__init__(scope, construct_id)
        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=security_group,
            idle_timeout=cdk.Duration.seconds(IDLE_TIMEOUT_SECONDS),
        )
        for key, value in access_log_attributes(access_logs.bucket, PUBLIC_PREFIX).items():
            self.load_balancer.set_attribute(key, value)
        self.load_balancer.node.add_dependency(access_logs.policy)
        self.https_listener: elbv2.ApplicationListener | None = None
        if not config.https_listener:
            return

        # Imported by its ID and the domain: no lookup at synthesis.
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self, "Zone", hosted_zone_id=config.hosted_zone_id, zone_name=config.domain
        )
        certificate = acm.Certificate(
            self,
            "Certificate",
            domain_name=config.domain,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        self.https_listener = self.load_balancer.add_listener(
            "Https",
            port=HTTPS_PORT,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[certificate],
            # Synthesized as ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09.
            ssl_policy=elbv2.SslPolicy.TLS13_12_RES_PQ,
            # The security group already admits HTTPS from anywhere (security.py): the listener
            # adds no rule of its own.
            open=False,
            default_action=elbv2.ListenerAction.fixed_response(NOT_FOUND),
        )
        # The CDK's generated target names its `bind` parameter `_record`, the protocol `record`:
        # ty reads them as different, and they are the same positional argument to jsii.
        alias = targets.LoadBalancerTarget(self.load_balancer)
        route53.ARecord(
            self,
            "Alias",
            zone=zone,
            target=route53.RecordTarget.from_alias(alias),  # ty: ignore[invalid-argument-type]
        )
