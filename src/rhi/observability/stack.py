"""The observability stack: the metrics workspace, and later the dashboards and the alarms that
read it (system design, section 7).

Other stacks of the stage take the workspace and its policies from `prometheus`; this stack
references none of them. The stack and the workspace's logical ID are recorded in
`tests/policy/stateful_logical_ids.json`: moving the workspace to another stack replaces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.observability.prometheus import PrometheusWorkspace

if TYPE_CHECKING:
    from constructs import Construct


class ObservabilityStack(cdk.Stack):
    """Holds the Managed Prometheus workspace, which the other stacks take from `prometheus`
    (`prometheus.workspace`, `prometheus.write_policy`, `prometheus.query_policy`)."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.prometheus = PrometheusWorkspace(self, "Prometheus")
