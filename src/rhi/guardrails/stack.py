"""The guardrails stack: the alerts topic, the cost budget, Cost Anomaly Detection behind its flag,
the fleet's alarms and the gateway's error-rate alarm (system design, sections 5.1, 7.3 and 11.3).

**It reads, and nothing reads it.** Its alarms read resources of the other stacks of the stage,
so no other stack may read a resource of this one: that would be a cycle between two stacks. A
topic that a construct of another stack publishes to lives in that stack. The stack declares no
output and exports nothing.

Each module of the stack defines a construct class, so that a cdk-nag finding on one of its
resources belongs to that module's acknowledgement file: `rhi.guardrails.alerts`,
`rhi.guardrails.budget`, `rhi.guardrails.anomaly`, `rhi.guardrails.alarms_fleet`, whose alarms
read the pools' groups from the pools stack, and `rhi.guardrails.alarms_gateway`, whose metric
filter and alarm read the gateway's log group, by its fixed name, and the public load balancer from
the network stack. The stack creates no resource of its own.

Parked (the configuration's `parked`, sections 5.6 and 11.2), there is no pool group, no scaler
and no gateway to watch: the stack holds no fleet alarm and no gateway alarm, and keeps the alerts
topic, the budget and Cost Anomaly Detection, since the account's cost still counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.gateway.service import LOG_GROUP_NAME
from rhi.guardrails.alarms_fleet import AlarmsFleet
from rhi.guardrails.alarms_gateway import AlarmsGateway
from rhi.guardrails.alerts import Alerts
from rhi.guardrails.anomaly import Anomaly
from rhi.guardrails.budget import Budget

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aws_cdk import aws_elasticloadbalancingv2 as elbv2
    from constructs import Construct

    from rhi.config import Config
    from rhi.pools.group import PoolGroup


class GuardrailsStack(cdk.Stack):
    """Holds the alerts topic and its key, as `alerts`, the cost budget, as `budget`, Cost Anomaly Detection, as `anomaly`, and the
    fleet's alarms over the pools' groups, `groups` by the pool's name, as `alarms_fleet`, and the
    gateway's error-rate alarm over the gateway's log group and the public `load_balancer`, as
    `alarms_gateway`. Both are None for a parked stage, which has no load balancer: `load_balancer`
    is then None."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        groups: Mapping[str, PoolGroup],
        load_balancer: elbv2.ApplicationLoadBalancer | None,
    ) -> None:
        super().__init__(scope, construct_id)
        # The alarms read the other stacks through their outputs, never their exports: an
        # export that this stack imported would stop the pools stack from removing a pool or
        # replacing its group, since the stage updates that stack before this one. An alarm that
        # names a group gone for the moment between the two updates harms nothing.
        cdk.CrossStackReferences.of(self).consume(cdk.ReferenceStrength.WEAK)
        self.alerts = Alerts(self, "Alerts", config=config)
        self.budget = Budget(self, "Budget", config=config, topic=self.alerts.topic)  # ty: ignore[invalid-argument-type] - as in `rhi.guardrails.alerts`
        self.anomaly = Anomaly(self, "Anomaly", config=config, topic=self.alerts.topic)  # ty: ignore[invalid-argument-type] - as in `rhi.guardrails.alerts`
        self.alarms_fleet: AlarmsFleet | None = None
        self.alarms_gateway: AlarmsGateway | None = None
        if not config.parked:
            self.alarms_fleet = AlarmsFleet(self, "AlarmsFleet", config=config, groups=groups, topic=self.alerts.topic)  # ty: ignore[invalid-argument-type] - as in `rhi.guardrails.alerts`
        if not config.parked and load_balancer is not None:
            self.alarms_gateway = AlarmsGateway(
                self,
                "AlarmsGateway",
                config=config,
                log_group_name=LOG_GROUP_NAME,
                load_balancer=load_balancer,
                topic=self.alerts.topic,  # ty: ignore[invalid-argument-type] - as in `rhi.guardrails.alerts`
            )
