"""The VPC of the endpoint (system design, section 6.5).

GPU instances and Fargate tasks run in private subnets and reach out through one NAT gateway:
without it they would need public addresses, and a security group would be the only thing between
the internet and an engine with unauthenticated routes. The public subnets hold the load balancer
and the NAT gateway. Instances read weights through the S3 gateway endpoint (section 4.4).

The VPC spans the zones it is given, at least three, with a public and a private subnet in each.
They are the zones of the stack that holds the VPC, which `rhi.network.stack` writes from the
configuration's `zones` so that CloudFormation resolves them in the deploying region at deployment
time. The CDK names each subnet after its zone's position in the list and allocates the CIDR blocks
in that order: a zone appended adds a subnet of each kind and changes none of the others, and a
zone moved or removed would replace subnets. `private_subnets` gives the private subnets at chosen
positions: a pool's group spans the subnets of its own zones only (section 5.4), where a load
balancer or a service selects the subnets by their type and spans every zone.

**Parked** (the configuration's `parked`, sections 5.6 and 11.2), the VPC has no NAT gateway: no
instance or task runs, and nothing reaches out. The CDK refuses a subnet with egress where there is
no NAT gateway, so the private subnets become isolated ones, under the same group name, `PRIVATE`:
a subnet, its route table and its association are named after their group and their zone, so they
keep their logical IDs, and the S3 endpoint, which selects them by that name, keeps its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_cdk import aws_ec2 as ec2
from constructs import Construct

if TYPE_CHECKING:
    from collections.abc import Sequence

# The CIDR of the VPC, private address space that names nothing.
CIDR = "10.0.0.0/16"
# The fewest zones the VPC spans, and the zones it spans when the configuration lists none: the
# deploying region's first three (`FIRST_ZONES` in `rhi.config`).
ZONES = 3
# The name of the private subnets' group, parked or not: their logical IDs are made from it.
PRIVATE = "Private"


class Network(Construct):
    """A VPC over the zones given, three or more, in their order, as `vpc`: a public and a private
    subnet in each, one NAT gateway, in the first zone's public subnet, and a gateway endpoint for
    S3 on the private subnets' route tables, as `s3_endpoint`. `parked`, no NAT gateway, and the
    private subnets are isolated, under the same names."""

    def __init__(
        self, scope: Construct, construct_id: str, *, availability_zones: Sequence[str], parked: bool = False
    ) -> None:
        super().__init__(scope, construct_id)
        if len(availability_zones) < ZONES:
            raise ValueError(f"the VPC spans at least {ZONES} zones, not {len(availability_zones)}")
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(CIDR),
            availability_zones=list(availability_zones),
            nat_gateways=0 if parked else 1,
            subnet_configuration=[
                # The load balancer's side, and the NAT gateway's. Neither needs an address given
                # at launch: the NAT gateway has its Elastic IP, the load balancer its own.
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24, map_public_ip_on_launch=False
                ),
                # Tasks and instances: no public address, outbound calls through the NAT gateway.
                # Parked, no NAT gateway and nothing that calls out: isolated.
                ec2.SubnetConfiguration(
                    name=PRIVATE,
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED if parked else ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=20,
                ),
            ],
        )
        # The endpoint instances read weights through. The model bucket's policy names it
        # (`rhi.pools.model_store`).
        self.s3_endpoint = self.vpc.add_gateway_endpoint(
            "S3",
            service=ec2.GatewayVpcEndpointAwsService.S3,
            subnets=[ec2.SubnetSelection(subnet_group_name=PRIVATE)],
        )


def private_subnets(vpc: ec2.Vpc, positions: Sequence[int]) -> list[ec2.ISubnet]:
    """The VPC's private subnets at these positions of its zones, in the order given: the VPC holds
    one private subnet per zone, in the order of its zones."""
    subnets = vpc.select_subnets(subnet_group_name=PRIVATE).subnets
    return [subnets[position] for position in positions]
