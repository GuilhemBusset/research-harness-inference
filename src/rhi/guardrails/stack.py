"""The guardrails stack: the alerts topic, and Cost Anomaly Detection behind its flag (system
design, sections 7.3 and 11.3).

**It reads, and nothing reads it.** Its alarms read resources of the other stacks of the stage,
so no other stack may read a resource of this one: that would be a cycle between two stacks. A
topic that a construct of another stack publishes to lives in that stack. The stack declares no
output and exports nothing.

Each module of the stack defines a construct class, so that a cdk-nag finding on one of its
resources belongs to that module's acknowledgement file: `rhi.guardrails.alerts` and
`rhi.guardrails.anomaly`. The stack creates no resource of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.guardrails.alerts import Alerts
from rhi.guardrails.anomaly import Anomaly

if TYPE_CHECKING:
    from constructs import Construct

    from rhi.config import Config


class GuardrailsStack(cdk.Stack):
    """Holds the alerts topic and its key, as `alerts`, and Cost Anomaly Detection, as `anomaly`."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config) -> None:
        super().__init__(scope, construct_id)
        self.alerts = Alerts(self, "Alerts", config=config)
        self.anomaly = Anomaly(self, "Anomaly", config=config, topic=self.alerts.topic)  # ty: ignore[invalid-argument-type] - as in `rhi.guardrails.alerts`
