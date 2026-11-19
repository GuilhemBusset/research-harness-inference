"""The observability stack: the metrics workspace, and later the dashboards and the alarms that
read it (system design, section 7).

Other stacks of the stage take the workspace and its policies from `prometheus`; this stack
references none of them. The stack and the workspace's logical ID are recorded in
`tests/policy/stateful_logical_ids.json`: moving the workspace to another stack replaces it.

The stack declares one output, `WorkspaceUrl`: the workspace's query address, which the pipeline's
verification takes as an input (`rhi.pipeline.verify`). The workspace also carries the tag
`STAGE_TAG`, whose value is the name of the stage that deploys it. The verification's role is
written before the workspace has an ID, so it names the workspace by that tag: it may query the
workspace of the stage it verifies and no other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk

from rhi.observability.prometheus import PrometheusWorkspace

if TYPE_CHECKING:
    from constructs import Construct

# The tag that says which stage a workspace belongs to.
STAGE_TAG = "rhi-stage"


class ObservabilityStack(cdk.Stack):
    """Holds the Managed Prometheus workspace, which the other stacks take from `prometheus`
    (`prometheus.workspace`, `prometheus.write_policy`, `prometheus.query_policy`), and the
    workspace's query address, as the output `workspace_url`."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.prometheus = PrometheusWorkspace(self, "Prometheus")
        stage = cdk.Stage.of(self)
        # A stack outside a stage belongs to none: the tag then names the stack itself.
        self.stage_name = stage.stage_name if stage is not None and stage.stage_name else self.stack_name
        cdk.Tags.of(self.prometheus.workspace).add(STAGE_TAG, self.stage_name)
        self.workspace_url = cdk.CfnOutput(
            self,
            "WorkspaceUrl",
            description="The metrics workspace's query address.",
            value=self.prometheus.workspace.attr_prometheus_endpoint,
        )
