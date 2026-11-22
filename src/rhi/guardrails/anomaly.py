"""Cost Anomaly Detection, beside the cost dashboard (system design, section 11.3), behind the
configuration's cost_anomaly_detection.

An account may hold only one monitor of the services dimension that AWS manages, and Cost
Explorer makes one for a new user: a second fails the deployment, and with it the merge. So the
construct creates nothing unless cost_anomaly_detection is true, which the owner sets once the
account's existing monitor is removed. When it is true:

- a monitor of the services dimension (`DIMENSIONAL`, `SERVICE`);
- a subscription to that monitor, by its ARN, that sends every anomaly to the alerts topic at once
  (`IMMEDIATE`), whatever its size: the design sets no threshold, so the total impact is at least
  0 dollars;
- a statement of the topic's policy that lets `costalerts.amazonaws.com` call `sns:Publish` on
  the topic, when the request comes from the stack's own account (`aws:SourceAccount`). Cost
  Anomaly Detection checks that it may publish when the subscription is created, so the
  subscription is created after the topic's policy.

The construct creates no role: its one grant is that statement of the topic's resource policy.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_ce as ce
from aws_cdk import aws_iam as iam
from constructs import Construct

if TYPE_CHECKING:
    from aws_cdk import aws_sns as sns

    from rhi.config import Config

PRINCIPAL = "costalerts.amazonaws.com"
# Every anomaly, whatever its total impact in dollars.
THRESHOLD = {
    "Dimensions": {
        "Key": "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
        "MatchOptions": ["GREATER_THAN_OR_EQUAL"],
        "Values": ["0"],
    }
}


class Anomaly(Construct):
    """The monitor, as `monitor`, and its subscription, as `subscription`, both None while
    cost_anomaly_detection is false."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config, topic: sns.ITopic) -> None:
        super().__init__(scope, construct_id)
        self.monitor: ce.CfnAnomalyMonitor | None = None
        self.subscription: ce.CfnAnomalySubscription | None = None
        if not config.cost_anomaly_detection:
            return
        grant = topic.add_to_resource_policy(
            iam.PolicyStatement(
                # ty reads the CDK's generated classes as unlike the protocols they implement where
                # a method names a parameter otherwise: jsii passes it by position.
                principals=[iam.ServicePrincipal(PRINCIPAL)],  # ty: ignore[invalid-argument-type]
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": cdk.Aws.ACCOUNT_ID}},
            )
        )
        self.monitor = ce.CfnAnomalyMonitor(
            self,
            "Monitor",
            monitor_name="rhi-services",
            monitor_type="DIMENSIONAL",
            monitor_dimension="SERVICE",
        )
        self.subscription = ce.CfnAnomalySubscription(
            self,
            "Subscription",
            subscription_name="rhi-alerts",
            frequency="IMMEDIATE",
            monitor_arn_list=[self.monitor.attr_monitor_arn],
            subscribers=[ce.CfnAnomalySubscription.SubscriberProperty(address=topic.topic_arn, type="SNS")],
            threshold_expression=json.dumps(THRESHOLD),
        )
        if grant.policy_dependable is not None:
            self.subscription.node.add_dependency(grant.policy_dependable)
