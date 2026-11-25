"""The gateway stack: the gateway's Fargate service (system design, sections 2 and 6.2).

The stack references the other stacks of the stage: the network's VPC, the gateway's security
group and the public load balancer's HTTPS listener, when it has one, the students' tables, the
workspace, and the pools' internal load balancer, target groups, secrets and pool-state table.
None of them references it back.

The stack declares one output, `Endpoint`: the endpoint's address, which the pipeline's
verification takes as an input (`rhi.pipeline.verify`). It is the configuration's domain while
`https_listener` is true and `NO_ENDPOINT` while it is false, when nothing listens there: the
output is declared either way, so that the verification's inputs never change shape.

Parked (the configuration's `parked`, sections 5.6 and 11.2), the stack holds no gateway service,
target group or listener rule, and the endpoint's output reads `NO_ENDPOINT`: a parked platform
answers nothing (section 6.2, step 8). The gateway's log group, whose name is fixed
(`rhi.gateway.service.log_group`), stays, and the stack then reads nothing of another stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from constructs import Construct

from rhi.gateway import service

if TYPE_CHECKING:
    from rhi.access.stack import AccessStack
    from rhi.config import Config
    from rhi.network.stack import NetworkStack
    from rhi.observability.stack import ObservabilityStack
    from rhi.pools.stack import PoolsStack

# What the endpoint's output reads while the public load balancer has no HTTPS listener.
NO_ENDPOINT = "none"


class GatewayStack(cdk.Stack):
    """Holds the gateway's service, which a later construct takes from `service`
    (`service.service`, `service.task_role`, `service.log_group`), reached through the network's
    HTTPS listener, or from nowhere while there is none, and the endpoint's address, as the
    output `endpoint`. Parked, `service` is None."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        network: NetworkStack,
        access: AccessStack,
        observability: ObservabilityStack,
        pools: PoolsStack,
    ) -> None:
        super().__init__(scope, construct_id)
        # What the service reads of the other stacks it imports through exports, the CDK's default,
        # stated here. A stack cannot drop an export while this one imports it: the pools stack
        # cannot remove a pool, or replace its target group, in the deployment that takes the pool
        # out of the gateway's environment, since the stage updates it before this stack.
        cdk.CrossStackReferences.of(self).consume(cdk.ReferenceStrength.STRONG)
        self.service: service.GatewayService | None = None
        if config.parked:
            # The gateway's log group alone, at the service's path.
            service.log_group(Construct(self, "Service"))
        else:
            self.service = self._service(config, network, access, observability, pools)
        self.endpoint = cdk.CfnOutput(
            self,
            "Endpoint",
            description="The endpoint's address, or none while it has no HTTPS listener.",
            value=config.domain if config.https_listener and not config.parked else NO_ENDPOINT,
        )

    def _service(
        self,
        config: Config,
        network: NetworkStack,
        access: AccessStack,
        observability: ObservabilityStack,
        pools: PoolsStack,
    ) -> service.GatewayService:
        assert pools.routing is not None, "an unparked stage has the internal load balancer"
        return service.GatewayService(
            self,
            "Service",
            config=config,
            vpc=network.vpc,
            security_group=network.security_groups.gateway,
            upstream=pools.routing.load_balancer,
            target_groups=pools.routing.target_groups,
            secrets=pools.secrets,
            keys=access.tables.keys,
            usage=access.tables.usage,
            pool_state=pools.tables.pool_state,
            workspace=observability.prometheus,
            listener=network.https_listener,
        )
