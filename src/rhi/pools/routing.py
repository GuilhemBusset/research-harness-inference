"""The internal load balancer: the pools' route allow-list, health checks and draining (system
design, sections 2, 4.1, 5.1, 5.2 and 5.3).

The engine cannot be trusted to protect itself: vLLM's key covers only some path prefixes, and
routes such as `/invocations` and `/pause` answer without it (evidence E23). This load balancer is
the control. Its one listener, HTTP on port 80 inside the VPC, forwards the four inference routes of
section 4.1, each for POST only, and answers a fixed 404 to everything else. The internal load
balancer's security group admits only the gateway's tasks (`rhi.network.security`), and the
listener adds no rule of its own to it.

A rule chooses its pool by the header `X-Rhi-Pool`, whose value is the pool's name: the gateway
sets it on every request it forwards and drops one a client sent, and no client reaches this load
balancer to set it.

Nothing probes an engine through the load balancer: each target group checks its engines' health
route directly, every 5 seconds as the cold-start estimate assumes (section 5.2), and the gateway
reads the target group's health through the AWS API (section 5.3). So the health route has no rule,
and is a 404 like any route outside the four. A target group routes by least outstanding requests
and drains for the pool's `drain_seconds` (section 5.1). Its targets are registered by the pool's
group, not here.

Stickiness per student (section 5.5) is phase 2's and is not set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from constructs import Construct

from rhi.network.security import ENGINE_PORT, INTERNAL_LOAD_BALANCER_PORT

if TYPE_CHECKING:
    from rhi_common.pool_config import Pool

# The inference routes of the engine contract (section 4.1), and the one method they take.
INFERENCE_ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens")
INFERENCE_METHOD = "POST"
# The header the gateway names the pool by (`rhi_gateway.proxy.POOL_HEADER`).
POOL_HEADER = "X-Rhi-Pool"
# The engine's health route (section 4.1), which only the target groups' health checks call.
HEALTH_ROUTE = "/health"

# Like the public load balancer's: nothing else in the path caps a long generation.
IDLE_TIMEOUT_SECONDS = 1200
HEALTH_CHECK_INTERVAL_SECONDS = 5
# A load balancer's health check must time out before its next one starts.
HEALTH_CHECK_TIMEOUT_SECONDS = 4
NOT_FOUND = 404
# Each pool's rules take priorities from a block of their own, in the order of the pools table.
PRIORITIES_PER_POOL = 10


class Routing(Construct):
    """The internal load balancer, as `load_balancer`, its listener, as `listener`, and one target
    group per pool, by the pool's name, as `target_groups`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        security_group: ec2.ISecurityGroup,
        pools: tuple[Pool, ...],
    ) -> None:
        super().__init__(scope, construct_id)
        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=False,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_group=security_group,
            idle_timeout=cdk.Duration.seconds(IDLE_TIMEOUT_SECONDS),
        )
        self.listener = self.load_balancer.add_listener(
            "Http",
            port=INTERNAL_LOAD_BALANCER_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            # The security group already admits the gateway on this port (security.py).
            open=False,
            default_action=elbv2.ListenerAction.fixed_response(NOT_FOUND),
        )
        self.target_groups: dict[str, elbv2.ApplicationTargetGroup] = {}
        for index, pool in enumerate(pools):
            target_group = elbv2.ApplicationTargetGroup(
                self,
                f"Pool-{pool.name}",
                vpc=vpc,
                port=ENGINE_PORT,
                protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.INSTANCE,
                load_balancing_algorithm_type=elbv2.TargetGroupLoadBalancingAlgorithmType.LEAST_OUTSTANDING_REQUESTS,
                deregistration_delay=cdk.Duration.seconds(pool.drain_seconds),
                health_check=elbv2.HealthCheck(
                    path=HEALTH_ROUTE,
                    port=str(ENGINE_PORT),
                    protocol=elbv2.Protocol.HTTP,
                    interval=cdk.Duration.seconds(HEALTH_CHECK_INTERVAL_SECONDS),
                    timeout=cdk.Duration.seconds(HEALTH_CHECK_TIMEOUT_SECONDS),
                ),
            )
            self.target_groups[pool.name] = target_group
            for offset, route in enumerate(INFERENCE_ROUTES, start=1):
                self.listener.add_action(
                    f"Pool-{pool.name}-Route{offset}",
                    priority=index * PRIORITIES_PER_POOL + offset,
                    conditions=[
                        elbv2.ListenerCondition.path_patterns([route]),
                        elbv2.ListenerCondition.http_request_methods([INFERENCE_METHOD]),
                        elbv2.ListenerCondition.http_header(POOL_HEADER, [pool.name]),
                    ],
                    action=elbv2.ListenerAction.forward([target_group]),
                )
