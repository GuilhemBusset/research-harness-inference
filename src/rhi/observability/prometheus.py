"""The Managed Prometheus workspace, and who may write and read it (system design, sections 6.6
and 7.1).

Every node, the gateway and the scaler push their own metrics to the workspace by remote write;
the dashboards query it. Per-student series exist only here, and section 6.6 keeps them until
course end plus 90 days. The workspace's retention counts from when each sample is written, so it
is 90 days: whatever was written before course end is gone 90 days after it.

The workspace is state. It has no deletion protection and no point-in-time recovery to turn on, so
it is retained on deletion and on replacement, and its logical ID is recorded in
`tests/policy/stateful_logical_ids.json`: renaming the construct replaces the workspace.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_aps as aps
from aws_cdk import aws_iam as iam
from constructs import Construct

# Section 6.6: per-student series are kept until course end plus 90 days.
RETENTION_DAYS = 90

# What a writer needs: Prometheus remote write, and nothing else.
WRITE_ACTIONS = ("aps:RemoteWrite",)
# What a reader of the PromQL query API needs: queries, and the series, labels and metadata a
# dashboard lists.
QUERY_ACTIONS = ("aps:QueryMetrics", "aps:GetSeries", "aps:GetLabels", "aps:GetMetricMetadata")


class PrometheusWorkspace(Construct):
    """The workspace, as `workspace`, and two policies that name it and no other resource:
    `write_policy`, for the roles that push metrics, and `query_policy`, for those that read them.
    Later constructs attach them to their roles.

    The construct IDs are part of the workspace's logical ID, which `tests/policy/` records."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)
        self.workspace = aps.CfnWorkspace(
            self,
            "Workspace",
            workspace_configuration=aps.CfnWorkspace.WorkspaceConfigurationProperty(
                retention_period_in_days=RETENTION_DAYS
            ),
        )
        self.workspace.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
        self.write_policy = iam.ManagedPolicy(
            self,
            "WritePolicy",
            description="Remote write to the metrics workspace.",
            statements=[iam.PolicyStatement(actions=list(WRITE_ACTIONS), resources=[self.workspace.attr_arn])],
        )
        self.query_policy = iam.ManagedPolicy(
            self,
            "QueryPolicy",
            description="Queries on the metrics workspace.",
            statements=[iam.PolicyStatement(actions=list(QUERY_ACTIONS), resources=[self.workspace.attr_arn])],
        )
