"""The network stack: the VPC, the security groups and the public load balancer (system design,
section 6.5).

The stack is environment-agnostic, and the CDK gives such a stack two zones. This one has three:
the first three that `Fn::GetAZs` lists for the deploying region, which CloudFormation resolves at
deployment time. No zone is named in the repository and nothing is looked up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.network.edge import Edge
from rhi.network.security import SecurityGroups
from rhi.network.vpc import ZONES, Network

if TYPE_CHECKING:
    from aws_cdk import aws_ec2 as ec2
    from aws_cdk import aws_elasticloadbalancingv2 as elbv2
    from constructs import Construct

    from rhi.config import Config


class NetworkStack(cdk.Stack):
    """Holds the VPC, the security groups and the public load balancer, which the other stacks of
    the stage take from `vpc`, `security_groups`, `load_balancer` and `https_listener`."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config) -> None:
        super().__init__(scope, construct_id)
        self.vpc: ec2.Vpc = Network(self, "Network", availability_zones=self.availability_zones).vpc
        # The groups of the load balancers, the tasks and the instances: another stack of the stage
        # takes one from `security_groups` (`security_groups.gateway`, ...).
        self.security_groups = SecurityGroups(self, "SecurityGroups", vpc=self.vpc)
        edge = Edge(
            self, "Edge", vpc=self.vpc, security_group=self.security_groups.public_load_balancer, config=config
        )
        self.load_balancer: elbv2.ApplicationLoadBalancer = edge.load_balancer
        # The listener a service adds its rule and its target group to, or None while the
        # configuration's `https_listener` is false: a service then adds neither.
        self.https_listener: elbv2.ApplicationListener | None = edge.https_listener
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
        """The deploying region's first three zones, as CloudFormation selects them."""
        return [cdk.Fn.select(index, cdk.Fn.get_azs()) for index in range(ZONES)]
