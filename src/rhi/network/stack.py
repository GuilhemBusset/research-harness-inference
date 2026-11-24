"""The network stack: the VPC, the security groups, the public load balancer and its web ACL
(system design, section 6.5), and the bucket of both load balancers' access logs (section 6.6).

The stack is environment-agnostic, and the CDK gives such a stack two zones. This one has the
zones the configuration lists (`zones`, at least three), in their order, and no template names one
in full (`zones`, below): CloudFormation resolves each in the deploying region at deployment time.

- The first three are the first three that `Fn::GetAZs` lists for the deploying region, whatever
  the configuration calls them: the owner's tool holds the configured first three to those by name
  before it writes the configuration (`rhi.owner_config`). A configuration that lists no zones has
  these three, so listing them changes nothing of the template.
- Each further zone is the deploying region followed by the last letter of the zone's name: a
  zone's name is its region's followed by one letter, and the configuration refuses two zones that
  end in the same letter (`rhi.config`).

A zone is only ever appended (`rhi.network.vpc`). Nothing is looked up.

**Parked** (the configuration's `parked`, sections 5.6 and 11.2), the stack holds no public load
balancer, listener, alias record or web ACL, and the VPC no NAT gateway (`rhi.network.vpc`). The
VPC, its subnets, the security groups, the S3 endpoint and the access-log bucket stay, and so does
the web ACL's log group, whose name is fixed (`rhi.network.waf.log_group`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from constructs import Construct

from rhi.network import waf
from rhi.network.access_logs import AccessLogs
from rhi.network.edge import Edge
from rhi.network.security import SecurityGroups
from rhi.network.vpc import ZONES, Network

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aws_cdk import aws_ec2 as ec2
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2
    from aws_cdk import aws_wafv2 as wafv2

    from rhi.config import Config


def zones(names: Sequence[str] | None) -> list[str]:
    """The zones of the VPC as the template writes them, in the order of the configuration's
    `zones`: the first three as `Fn::Select` of positions 0 to 2 over `Fn::GetAZs`, and each
    further one as `Fn::Join` of `AWS::Region` and the last letter of its name. With no names, the
    first three alone."""
    first = [cdk.Fn.select(index, cdk.Fn.get_azs()) for index in range(ZONES)]
    return first + [cdk.Fn.join("", [cdk.Aws.REGION, name[-1]]) for name in (names or ())[ZONES:]]


class NetworkStack(cdk.Stack):
    """Holds the VPC, its S3 gateway endpoint, the security groups and the public load balancer,
    which the other stacks of the stage take from `vpc`, `s3_endpoint`, `security_groups`,
    `load_balancer` and `https_listener`, and the bucket of the load balancers' access logs, from
    `access_logs` (`access_logs.bucket`). Parked, `load_balancer`, `https_listener` and `web_acl`
    are None."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config) -> None:
        super().__init__(scope, construct_id)
        # The configuration's zones, by name, before the VPC reads `availability_zones`.
        self._zone_names = config.zones
        network = Network(self, "Network", availability_zones=self.availability_zones, parked=config.parked)
        self.vpc: ec2.Vpc = network.vpc
        # The endpoint instances read weights through, which the model bucket's policy names.
        self.s3_endpoint: ec2.GatewayVpcEndpoint = network.s3_endpoint
        # The groups of the load balancers, the tasks and the instances: another stack of the stage
        # takes one from `security_groups` (`security_groups.gateway`, ...).
        self.security_groups = SecurityGroups(self, "SecurityGroups", vpc=self.vpc)
        # Both load balancers' access logs, the internal one's included: it is in the pools stack,
        # which deploys after this one, and so after the bucket's policy.
        self.access_logs = AccessLogs(self, "AccessLogs")
        self.load_balancer: elbv2.ApplicationLoadBalancer | None = None
        # The listener a service adds its rule and its target group to, or None while the
        # configuration's `https_listener` is false: a service then adds neither.
        self.https_listener: elbv2.ApplicationListener | None = None
        self.web_acl: wafv2.CfnWebACL | None = None
        if config.parked:
            # No load balancer and no ACL: the ACL's log group alone, at the ACL's path.
            waf.log_group(Construct(self, "Waf"))
        else:
            edge = Edge(
                self,
                "Edge",
                vpc=self.vpc,
                security_group=self.security_groups.public_load_balancer,
                access_logs=self.access_logs,
                config=config,
            )
            self.load_balancer = edge.load_balancer
            self.https_listener = edge.https_listener
            # The web ACL limits rates on the load balancer, with the listener on or off (waf.py).
            self.web_acl = waf.WebAcl(
                self, "Waf", load_balancer=edge.load_balancer, rate_limits=config.rate_limits
            ).web_acl
        # The VPC strips its default security group of every rule (the flag of `cdk.json`), through
        # a function the CDK puts at the stack's root. That function names its role's ARN, which
        # orders the two already; its `DependsOn` on the role repeats it, and cfn-lint warns
        # (W3005).
        provider = self.node.try_find_child("Custom::VpcRestrictDefaultSGCustomResourceProvider")
        if provider is not None:
            handler = provider.node.find_child("Handler")
            assert isinstance(handler, cdk.CfnResource)
            handler.add_deletion_override("DependsOn")

    @property
    def availability_zones(self) -> list[str]:
        """The zones the configuration lists, or the deploying region's first three, as
        CloudFormation resolves them (`zones`)."""
        return zones(self._zone_names)
