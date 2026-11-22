"""The guardrails stack: the alerts topic, the cost budget, Cost Anomaly Detection behind its flag,
and the fleet's alarms (system design, sections 5.1, 7.3 and 11.3).

**It reads, and nothing reads it.** Its alarms read resources of the other stacks of the stage,
so no other stack may read a resource of this one: that would be a cycle between two stacks. A
topic that a construct of another stack publishes to lives in that stack. The stack declares no
output and exports nothing.

Each module of the stack defines a construct class, so that a cdk-nag finding on one of its
resources belongs to that module's acknowledgement file: `rhi.guardrails.alerts`,
`rhi.guardrails.budget`, `rhi.guardrails.anomaly` and `rhi.guardrails.alarms_fleet`, whose alarms
read the pools' groups from the pools stack. The stack creates no resource of its own.

Parked (the configuration's `parked`, sections 5.6 and 11.2), there is no pool group, no scaler
and no gateway to watch: the stack holds no fleet alarm, and keeps the alerts topic, the budget
and Cost Anomaly Detection, since the account's cost still counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.guardrails.alarms_fleet import AlarmsFleet
from rhi.guardrails.alerts import Alerts
from rhi.guardrails.anomaly import Anomaly
from rhi.guardrails.budget import Budget

if TYPE_CHECKING:
    from collections.abc import Mapping

    from constructs import Construct

    from rhi.config import Config
    from rhi.pools.group import PoolGroup


class GuardrailsStack(cdk.Stack):
    """Holds the alerts topic and its key, as `alerts`, the cost budget, as `budget`, Cost Anomaly Detection, as `anomaly`, and the
    fleet's alarms over the pools' groups, `groups` by the pool's name, as `alarms_fleet`, which is
    None for a parked stage."""

    def __init__(
        self, scope: Construct, construct_id: str, *, config: Config, groups: Mapping[str, PoolGroup]
    ) -> None:
        super().__init__(scope, construct_id)
        # The alarms read the pools' groups through the pools stack's outputs, never its exports: an
        # export that this stack imported would stop the pools stack from removing a pool or
        # replacing its group, since the stage updates that stack before this one. An alarm that
        # names a group gone for the moment between the two updates harms nothing.
        cdk.CrossStackReferences.of(self).consume(cdk.ReferenceStrength.WEAK)
        self.alerts = Alerts(self, "Alerts", config=config)
        self.budget = Budget(self, "Budget", config=config, topic=self.alerts.topic)  # ty: ignore[invalid-argument-type] - as in `rhi.guardrails.alerts`
        self.anomaly = Anomaly(self, "Anomaly", config=config, topic=self.alerts.topic)  # ty: ignore[invalid-argument-type] - as in `rhi.guardrails.alerts`
        self.alarms_fleet: AlarmsFleet | None = None
        if not config.parked:
            self.alarms_fleet = AlarmsFleet(self, "AlarmsFleet", config=config, groups=groups, topic=self.alerts.topic)  # ty: ignore[invalid-argument-type] - as in `rhi.guardrails.alerts`
