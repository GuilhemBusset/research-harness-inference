"""AWS Budgets, alerts only (system design, section 11.3): a monthly cost budget that e-mails the
admin, through the alerts topic, at thresholds of a monthly amount, and nothing that stops.

- **A monthly budget of cost** for the configuration's monthly_budget_usd, in US dollars, read at
  synthesis from the configuration in SSM, never from the repository.
- **One notification on actual cost at each threshold**, each percentage of the amount that the
  configuration's budget_alert_percentages lists (50%, 80% and 100% when it is left out, section
  17), each to the alerts topic. Budgets' data is updated about once a day (evidence E27).
- **A statement of the topic's policy** that lets `budgets.amazonaws.com` call `sns:Publish` on the
  topic, when the request comes from the stack's own account (`aws:SourceAccount`). The topic's key
  already lets AWS Budgets use it (`rhi.guardrails.alerts`). The budget is created after the
  statement, since Budgets checks that it may publish when a notification is made.

No latch, no second topic, no function, no role, no action on any resource. Parked (sections 5.6
and 11.2) the budget stays, since the account's cost still counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_iam as iam
from constructs import Construct

if TYPE_CHECKING:
    from aws_cdk import aws_sns as sns

    from rhi.config import Config

PRINCIPAL = "budgets.amazonaws.com"


class Budget(Construct):
    """The monthly cost budget, as `budget`, with its notifications to `topic`."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config, topic: sns.ITopic) -> None:
        super().__init__(scope, construct_id)
        grant = topic.add_to_resource_policy(
            iam.PolicyStatement(
                # Its own statement, which the CDK merges with no other grant.
                sid="Budgets",
                # ty reads the CDK's generated classes as unlike the protocols they implement where
                # a method names a parameter otherwise: jsii passes it by position.
                principals=[iam.ServicePrincipal(PRINCIPAL)],  # ty: ignore[invalid-argument-type]
                actions=["sns:Publish"],
                resources=[topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": cdk.Aws.ACCOUNT_ID}},
            )
        )
        self.budget = budgets.CfnBudget(
            self,
            "Budget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=config.monthly_budget_usd, unit="USD"),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=percentage,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[budgets.CfnBudget.SubscriberProperty(address=topic.topic_arn, subscription_type="SNS")],
                )
                for percentage in config.budget_alert_percentages
            ],
        )
        if grant.policy_dependable is not None:
            self.budget.node.add_dependency(grant.policy_dependable)
