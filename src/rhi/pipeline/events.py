"""The pipeline's events, kept in a log group of their own, which Grafana reads (system design,
sections 7.2 and 7.4).

CodePipeline sends its state changes of executions, stages and actions to EventBridge. A rule on
the default bus matches those of this pipeline and no other, the same three kinds the reporter
listens to (`rhi.pipeline.reporter.DETAIL_TYPES`), and its one target is the log group
`LOG_GROUP_NAME`. The deployments dashboard reads the runs and their results there.

**A fixed name.** The log group is in this stack and Grafana in the stage, another assembly: the
role of Grafana's task names it by this name, as text, and holds no reference to this stack.
Grafana reads no build project's log group, which holds real configuration; a state-change event
carries no configuration value.

**Its retention** is the configuration's pipeline_events_retention_days, 90 days by default
(section 6.6): the events hold no personal data, and the design gives no other period.

**EventBridge writes to it under a resource policy of CloudWatch Logs**, which lets the two
service principals that AWS documents for a log-group target, `events.amazonaws.com` and
`delivery.logs.amazonaws.com`, create streams in this log group and put events to them, and
nothing else. CDK's log-group target would install that policy through a custom resource, with a
function and a role of its own; here the policy is a resource of the template, so the stack gains
no function and no role. The rule waits for the policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from rhi.pipeline.reporter import DETAIL_TYPES

if TYPE_CHECKING:
    from aws_cdk import aws_codepipeline as codepipeline

    from rhi.config import Config

# The log group's fixed name, by which Grafana's role names it (`rhi.observability.grafana`).
LOG_GROUP_NAME = "/rhi/pipeline/events"
# The resource policy's fixed name: CloudWatch Logs keeps an account's policies by name.
POLICY_NAME = "rhi-pipeline-events"
# What a log-group target of EventBridge needs on the log group, and who writes for it.
WRITE_ACTIONS = ("logs:CreateLogStream", "logs:PutLogEvents")
WRITERS = ("events.amazonaws.com", "delivery.logs.amazonaws.com")


class PipelineEvents(Construct):
    """The log group of `pipeline`'s state changes, as `log_group`, its resource policy, as
    `policy`, and the rule that sends them to it, as `rule`."""

    def __init__(
        self, scope: Construct, construct_id: str, *, config: Config, pipeline: codepipeline.IPipeline
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)

        # The resource itself: its retention is a number of days that the configuration gives,
        # which the CDK's enumeration of periods does not take as a number.
        self.log_group = logs.CfnLogGroup(
            self,
            "Logs",
            log_group_name=LOG_GROUP_NAME,
            retention_in_days=config.pipeline_events_retention_days,
        )
        self.log_group.apply_removal_policy(cdk.RemovalPolicy.DESTROY)
        # The log group's ARN, as CloudFormation gives it, ends `:*`: the log group and its streams.
        # ty reads the CDK's generated principal as unlike its protocol where a method names a
        # parameter otherwise: jsii passes it by position.
        writers = [iam.ServicePrincipal(writer) for writer in WRITERS]
        self.policy = logs.ResourcePolicy(
            self,
            "Policy",
            resource_policy_name=POLICY_NAME,
            policy_statements=[
                iam.PolicyStatement(
                    actions=list(WRITE_ACTIONS),
                    principals=writers,  # ty: ignore[invalid-argument-type]
                    resources=[self.log_group.attr_arn],
                )
            ],
        )
        # The target is the log group's ARN without `:*`, as EventBridge names a log group.
        target = stack.format_arn(
            service="logs",
            resource="log-group",
            resource_name=self.log_group.ref,
            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
        )
        self.rule = events.CfnRule(
            self,
            "Rule",
            description="Keeps this pipeline's state changes in its log group.",
            event_pattern={
                "source": ["aws.codepipeline"],
                "detail-type": list(DETAIL_TYPES),
                "resources": [pipeline.pipeline_arn],
            },
            state="ENABLED",
            targets=[events.CfnRule.TargetProperty(id="LogGroup", arn=target)],
        )
        self.rule.node.add_dependency(self.policy)
