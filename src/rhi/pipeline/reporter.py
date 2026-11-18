"""The pipeline's reporter: an EventBridge rule on this pipeline's state, the measurement bus with a
rule on it, and the function both rules invoke (system design, sections 8.2, step 7, 8.4 and 12).

The rule matches CodePipeline's state changes of executions, stages and actions, of this pipeline
and no other. Its one target is the function of `services/reporter/` (`rhi_reporter.handler`),
which turns each event into commit statuses on the commit the execution deploys.

**The measurement bus.** A spike, the weekly GPU canary or the dry run runs as a job that cannot
post a status itself: the App's key is in the reporter's secret, which only the function may
read, and a job holds no other GitHub credential. It hands each run of a measurement to the
reporter as one event of source `rhi.measurement` on an EventBridge bus of its own,
`MEASUREMENT_BUS_NAME`, which keeps a job's grant to one resource and its events apart from the
pipeline's. A second rule, on that bus only, invokes the function for those events. A job's
construct calls `measuring_job`: it lets the job's role put events on that bus and do nothing else,
and gives the job the bus's name in `RHI_MEASUREMENT_BUS`. It grants nothing on the secret.

**The secret is referred to, never created.** The function's GitHub credential is the status
reporter App of section 8.6, step 3, whose private key only the owner holds. It is read from the
secret `SECRET_NAME`, which the owner creates with the fields app_id, installation_id and
private_key. A secret the stack created would hold a placeholder the function would have to tell
from a real value, and would be a stateful resource of this stack. While it is absent, the
function posts nothing.

**The function's role** may read that secret, the state of this pipeline's executions and their
list, from which it finds the commits a self-update left without an execution (section 8.4), the
events of the stacks this pipeline deploys, the objects of the pipeline's artifact bucket (where the
synth step's output, the templates of each execution, is kept), publish to the owner's topic of
`rhi.pipeline.notify` and to no other topic, and write its own logs. Nothing else:
`tests/policy/test_pipeline.py` holds it to that. The topic is encrypted with the key AWS manages
for SNS, whose policy admits the account's principals through SNS: the role needs no grant on it.

What the function is told, as environment variables:

- `RHI_REPORTER_SECRET`: the secret's name;
- `RHI_REPORTER_REPOSITORY`: the repository, as owner/name, from the configuration;
- `RHI_REPORTER_STAGES`: the stages the pipeline deploys, comma-separated, under which the steps
  that run before them report;
- `RHI_REPORTER_TOPIC`: the ARN of the owner's topic, where the details of a failure go;
- `RHI_REPORTER_SMOKE_TESTS`: the allow-list of smoke test IDs, comma-separated: the list of
  `tests/smoke/ids.py`, read at synthesis (`smoke_test_ids`). The function's bundle holds nothing
  of the tests, and a report names a failed smoke test only by an ID of this list (section 8.4).
"""

from __future__ import annotations

import runpy
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
    from collections.abc import MutableMapping
    from pathlib import Path

    from aws_cdk import aws_codepipeline as codepipeline
    from aws_cdk import aws_sns as sns

    from rhi.config import Config

# The secret that holds the status reporter App's credential. The owner creates it.
SECRET_NAME = "rhi/reporter/github-app"

SECRET_VARIABLE = "RHI_REPORTER_SECRET"
REPOSITORY_VARIABLE = "RHI_REPORTER_REPOSITORY"
STAGES_VARIABLE = "RHI_REPORTER_STAGES"
TOPIC_VARIABLE = "RHI_REPORTER_TOPIC"
SMOKE_TESTS_VARIABLE = "RHI_REPORTER_SMOKE_TESTS"

# Where the smoke tests list their IDs, and the name the list has there.
SMOKE_TEST_IDS_FILE = packaging.REPO / "tests/smoke/ids.py"
SMOKE_TEST_IDS_NAME = "SMOKE_TEST_IDS"
# What separates two IDs in the variable.
SEPARATOR = ","

# The three kinds of state change CodePipeline sends to EventBridge.
DETAIL_TYPES = [
    "CodePipeline Pipeline Execution State Change",
    "CodePipeline Stage Execution State Change",
    "CodePipeline Action Execution State Change",
]

# The measurement bus: its fixed name, the source of a job's events, and the variable that names the
# bus to a job (`rhi_reporter.measure`).
MEASUREMENT_BUS_NAME = "rhi-measurements"
MEASUREMENT_SOURCE = "rhi.measurement"
MEASUREMENT_BUS_VARIABLE = "RHI_MEASUREMENT_BUS"

HANDLER = "rhi_reporter.handler.handler"
ARCHITECTURE = lambda_.Architecture.ARM_64
# Enough to hold the synth step's output, read whole, while a failure is reported.
MEMORY_MB = 512
TIMEOUT = cdk.Duration.minutes(1)
LOG_RETENTION = logs.RetentionDays.ONE_MONTH


def smoke_test_ids(path: Path = SMOKE_TEST_IDS_FILE) -> tuple[str, ...]:
    """The ID of every smoke test, as `tests/smoke/ids.py` lists them: the reporter's allow-list.
    The file is data and imports nothing. ValueError for an ID the variable cannot carry."""
    ids = tuple(runpy.run_path(str(path))[SMOKE_TEST_IDS_NAME])
    if not all(isinstance(test_id, str) and test_id and SEPARATOR not in test_id for test_id in ids):
        raise ValueError(f"a smoke test's ID is text with no {SEPARATOR!r} in it")
    return ids


class Reporter(Construct):
    """The rule on `pipeline`'s state and the function that reports it, for `stage`, the stage the
    pipeline deploys, and sends the details of a failure to `topic`."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Config,
        pipeline: codepipeline.Pipeline,
        stage: cdk.Stage,
        topic: sns.ITopic,
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
        # The owner's topic, and no other.
        role.add_to_policy(iam.PolicyStatement(actions=["sns:Publish"], resources=[topic.topic_arn]))

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
                TOPIC_VARIABLE: topic.topic_arn,
                SMOKE_TESTS_VARIABLE: SEPARATOR.join(smoke_test_ids()),
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
        # The measurement bus, and a rule on it for a job's events, which invokes the same function.
        self.measurement_bus = events.EventBus(self, "MeasurementBus", event_bus_name=MEASUREMENT_BUS_NAME)
        self.measurement_rule = events.Rule(
            self,
            "MeasurementRule",
            event_bus=self.measurement_bus,
            event_pattern=events.EventPattern(source=[MEASUREMENT_SOURCE]),
            targets=[target],  # ty: ignore[invalid-argument-type] - as above
        )


def measuring_job(role: iam.Role | iam.IRole, environment: MutableMapping[str, str]) -> None:
    """Lets a job post measurements: `role`, the job's, may put events on the measurement bus and
    on nothing else, and `environment`, the variables the job's construct gives the job, names the
    bus in `RHI_MEASUREMENT_BUS`. A CodeBuild step of the pipeline, for one, is given the role as its
    `role` and the variables as its `env`. Nothing is granted on the reporter's secret.

    The bus is named by its fixed name, in the account and region of the role's stack, so a job of
    any stack of the app can be given it."""
    bus = cdk.Stack.of(role).format_arn(service="events", resource="event-bus", resource_name=MEASUREMENT_BUS_NAME)
    added = role.add_to_principal_policy(iam.PolicyStatement(actions=["events:PutEvents"], resources=[bus]))
    if not added.statement_added:
        raise ValueError("the job's role takes no statement: the role is not one of the app's")
    environment[MEASUREMENT_BUS_VARIABLE] = MEASUREMENT_BUS_NAME
