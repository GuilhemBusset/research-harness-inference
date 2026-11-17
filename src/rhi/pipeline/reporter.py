"""The pipeline's reporter: an EventBridge rule on this pipeline's state and the function it invokes
(system design, section 8.2, step 7, and section 8.4).

The rule matches CodePipeline's state changes of executions, stages and actions, of this pipeline
and no other. Its one target is the function of `services/reporter/` (`rhi_reporter.handler`),
which turns each event into commit statuses on the commit the execution deploys.

**The secret is referred to, never created.** The function's GitHub credential is the status
reporter App of section 8.6, step 3, whose private key only the owner holds. It is read from the
secret `SECRET_NAME`, which the owner creates with the fields app_id, installation_id and
private_key. A secret the stack created would hold a placeholder the function would have to tell
from a real value, and would be a stateful resource of this stack. While it is absent, the
function posts nothing.

**The function's role** may read that secret, the state of this pipeline's executions and their
list, from which it finds the commits a self-update left without an execution (section 8.4), the
events of the stacks this pipeline deploys, the objects of the pipeline's artifact bucket (where the
synth step's output, the templates of each execution, is kept), and write its own logs. Nothing
else: `tests/policy/test_pipeline.py` holds it to that.

What the function is told, as environment variables:

- `RHI_REPORTER_SECRET`: the secret's name;
- `RHI_REPORTER_REPOSITORY`: the repository, as owner/name, from the configuration;
- `RHI_REPORTER_STAGES`: the stages the pipeline deploys, comma-separated, under which the steps
  that run before them report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from rhi import packaging

if TYPE_CHECKING:
    from aws_cdk import aws_codepipeline as codepipeline

    from rhi.config import Config

# The secret that holds the status reporter App's credential. The owner creates it.
SECRET_NAME = "rhi/reporter/github-app"

SECRET_VARIABLE = "RHI_REPORTER_SECRET"
REPOSITORY_VARIABLE = "RHI_REPORTER_REPOSITORY"
STAGES_VARIABLE = "RHI_REPORTER_STAGES"

# The three kinds of state change CodePipeline sends to EventBridge.
DETAIL_TYPES = [
    "CodePipeline Pipeline Execution State Change",
    "CodePipeline Stage Execution State Change",
    "CodePipeline Action Execution State Change",
]

HANDLER = "rhi_reporter.handler.handler"
ARCHITECTURE = lambda_.Architecture.ARM_64
# Enough to hold the synth step's output, read whole, while a failure is reported.
MEMORY_MB = 512
TIMEOUT = cdk.Duration.minutes(1)
LOG_RETENTION = logs.RetentionDays.ONE_MONTH


class Reporter(Construct):
    """The rule on `pipeline`'s state and the function that reports it, for `stage`, the stage the
    pipeline deploys."""

    def __init__(
        self, scope: Construct, construct_id: str, *, config: Config, pipeline: codepipeline.Pipeline, stage: cdk.Stage
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)

        # ty reads the CDK's generated classes as unlike the protocols they implement where a method
        # names a parameter otherwise (`grantee` for `identity`): jsii passes it by position.
        lambda_service = iam.ServicePrincipal("lambda.amazonaws.com")
        # Its own role, with no managed policy: what the function may do is listed here, whole.
        role = iam.Role(self, "Role", assumed_by=lambda_service)  # ty: ignore[invalid-argument-type]
        log_group = logs.LogGroup(self, "Logs", retention=LOG_RETENTION, removal_policy=cdk.RemovalPolicy.DESTROY)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[log_group.log_group_arn]
            )
        )
        # Secrets Manager adds six random characters to a secret's name in its ARN.
        secret = stack.format_arn(
            service="secretsmanager",
            resource="secret",
            resource_name=f"{SECRET_NAME}-??????",
            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
        )
        role.add_to_policy(iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"], resources=[secret]))
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "codepipeline:GetPipelineExecution",
                    "codepipeline:GetPipelineState",
                    "codepipeline:ListActionExecutions",
                    "codepipeline:ListPipelineExecutions",
                ],
                resources=[pipeline.pipeline_arn],
            )
        )
        # The stacks of the stage, by the names the pipeline deploys them under, and no other.
        deployed = sorted(
            {construct.stack_name for construct in stage.node.find_all() if isinstance(construct, cdk.Stack)}
        )
        stack_arns = [
            stack.format_arn(service="cloudformation", resource="stack", resource_name=f"{name}/*") for name in deployed
        ]
        role.add_to_policy(iam.PolicyStatement(actions=["cloudformation:DescribeStackEvents"], resources=stack_arns))
        # The synth step's output of every execution, in the pipeline's own artifact bucket.
        role.add_to_policy(
            iam.PolicyStatement(actions=["s3:GetObject"], resources=[pipeline.artifact_bucket.arn_for_objects("*")])
        )

        self.function = lambda_.Function(
            self,
            "Function",
            runtime=packaging.RUNTIME,
            architecture=ARCHITECTURE,
            handler=HANDLER,
            code=packaging.function_code("reporter", ARCHITECTURE),
            role=role,  # ty: ignore[invalid-argument-type] - as above, a generated class read as unlike its protocol
            log_group=log_group,
            memory_size=MEMORY_MB,
            timeout=TIMEOUT,
            environment={
                SECRET_VARIABLE: SECRET_NAME,
                REPOSITORY_VARIABLE: f"{config.github_owner}/{config.github_repo}",
                STAGES_VARIABLE: stage.stage_name,
            },
        )
        target = targets.LambdaFunction(self.function)  # ty: ignore[invalid-argument-type] - as above
        self.rule = events.Rule(
            self,
            "Rule",
            event_pattern=events.EventPattern(
                source=["aws.codepipeline"], detail_type=DETAIL_TYPES, resources=[pipeline.pipeline_arn]
            ),
            targets=[target],  # ty: ignore[invalid-argument-type] - as above
        )
