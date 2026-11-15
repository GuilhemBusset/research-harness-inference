"""The security groups of the endpoint, and who reaches whom (system design, sections 4.1 and 6.5).

The public load balancer is the only thing that faces the internet: it admits HTTPS from anywhere.
It reaches the gateway's tasks and Grafana's tasks; the issuer is a function behind it and needs no
group. Only the gateway reaches the internal load balancer, and only the internal load balancer
reaches the engine's port on the pools' instances. No group of a task or an instance admits an
address range, and `tests/policy/test_no_public_ingress.py` asserts it.

The ports are fixed here, and the services that listen on them use the same numbers: the engine's
8000 (section 4.1), the gateway's 8080, Grafana's own default 3000, and 80 on the internal load
balancer.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

HTTPS_PORT = 443
GATEWAY_PORT = 8080
GRAFANA_PORT = 3000
INTERNAL_LOAD_BALANCER_PORT = 80
ENGINE_PORT = 8000

# The tag that marks the public load balancer's group, the one group the policy tests let admit
# the internet (on HTTPS only).
EDGE_TAG = "rhi-edge"
EDGE_VALUE = "public-load-balancer"


class SecurityGroups(Construct):
    """One security group each for the public load balancer, the gateway's tasks, Grafana's tasks,
    the internal load balancer and the pools' instances, each admitting one port from one source."""

    def __init__(self, scope: Construct, construct_id: str, *, vpc: ec2.IVpc) -> None:
        super().__init__(scope, construct_id)

        # A group's description admits no apostrophe (CloudFormation's pattern for it).
        def group(name: str, description: str) -> ec2.SecurityGroup:
            return ec2.SecurityGroup(self, name, vpc=vpc, description=description)

        self.public_load_balancer = group("PublicLoadBalancer", "The public load balancer: HTTPS from anywhere")
        cdk.Tags.of(self.public_load_balancer).add(EDGE_TAG, EDGE_VALUE)
        self.gateway = group("Gateway", "Gateway tasks: from the public load balancer")
        self.grafana = group("Grafana", "Grafana tasks: from the public load balancer")
        self.internal_load_balancer = group("InternalLoadBalancer", "The internal load balancer: from the gateway")
        self.instances = group("Instances", "Pool instances: the engine port from the internal load balancer")

        self.public_load_balancer.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(HTTPS_PORT), "HTTPS from the internet: this is the edge"
        )
        self.gateway.add_ingress_rule(
            self.public_load_balancer, ec2.Port.tcp(GATEWAY_PORT), "The gateway, from the public load balancer"
        )
        self.grafana.add_ingress_rule(
            self.public_load_balancer, ec2.Port.tcp(GRAFANA_PORT), "Grafana, from the public load balancer"
        )
        self.internal_load_balancer.add_ingress_rule(
            self.gateway, ec2.Port.tcp(INTERNAL_LOAD_BALANCER_PORT), "The internal load balancer, from the gateway"
        )
        self.instances.add_ingress_rule(
            self.internal_load_balancer, ec2.Port.tcp(ENGINE_PORT), "The engine, from the internal load balancer"
        )
