"""The weekly GPU canary: a job that runs the coursework pool's smoke tests against the deployed
endpoint, and the schedule that starts it (system design, sections 8.2, step 6, 11.2 and 14).

**What it starts.** One EventBridge rule, `rhi-gpu-canary`, on a schedule, whose one target is the
canary's CodeBuild project, `rhi-gpu-canary`. The rule's role may start a build of that project
and nothing else. The job runs `scripts/pipeline/canary_result.py` on the head of `main`: it
books one instance of the coursework pool, under the name `pipeline/canary`, when the pool has no
session, waits until the pool reads ready or the configuration's `check_wait_minutes` have passed,
runs the smoke tests of `tests/smoke` against the deployed endpoint, on that pool, with the
canary's key, stops the session it booked, and sends the measurement `gpu-canary` of the result to
the reporter through the measurement bus. It starts no execution of the pipeline and deploys
nothing: it tests what is deployed, whatever was deployed, and the job has no webhook.

**When.** Once a week, on Sundays at 04:00 UTC, `SCHEDULE`: an EventBridge rule reads its cron
expression in UTC. Section 8.5 holds deployments outside session windows; the canary is not a
deployment, so the hold does not apply to it. Neither the configuration nor the design gives a
time: this module fixes it.

**How long the job runs.** Its timeout is `timeout`: `TIMEOUT`, which covers the set-up, the smoke
tests, the measurement and the stop of the session, and `check_wait_minutes` more, the wait for the
session's instance. So whatever the configuration's wait is, the build is still running when the
wait is over, the result is told and the session is stopped. A wait that would take the timeout
beyond what CodeBuild lets one build run fails the synthesis.

**While the system is parked** (the configuration's `parked`, sections 8.5 and 11.2), there is no
endpoint to test, and the canary does not run: the construct creates no schedule, and with it
neither the job nor its role, so that nothing is left that could start a build, by hand or
otherwise, and the rule of `tests/policy/test_pipeline.py` that gives the canary's job exactly one
schedule holds in the parked synthesis too. Only the job's log group stays, under its logical ID,
with the logs of the runs before; unparked, the job, its role and its schedule are created again
as they were. The pipeline is synthesized from the stage's configuration, so the flag is known
here at synthesis.

**What the job is told**, as text built at synthesis, since this stack holds no reference to a
resource of the stage:

- `RHI_SMOKE_ENDPOINT`: the endpoint's address, the value of the gateway stack's output, which
  reads `none` while the endpoint has no HTTPS listener;
- `RHI_SMOKE_RELEASE_PARAMETERS`: each pool's release parameter, as JSON from the pool's name to
  the parameter's name, which the smoke tests read, and whose settings, for the coursework pool,
  give the script the stage's Prometheus workspace when it runs;
- `RHI_VERIFY_POOLS_STACK` and `RHI_VERIFY_POOL_STATE_LOGICAL_ID`: the pools stack's name and the
  logical ID of the pool-state table in it, by which the job finds the table's name, as the
  deployment hold does (`rhi.pipeline.hold`);
- `RHI_VERIFY_CHECK_WAIT_MINUTES` and `RHI_VERIFY_SESSION_MAX_HOURS`: the configuration's
  `check_wait_minutes` and `session_max_hours`, the wait for the session's instance and the bound a
  session is written and read under. The four are the verification's own variables
  (`rhi.pipeline.verify`), since the job books with the verification's routine;
- `RHI_MEASUREMENT_BUS`: the measurement bus, through `rhi.pipeline.reporter.measuring_job`;
- `PYTHONPATH`: the repository's package roots, `rhi.pipeline.synth.import_path`.

**The role** is the verification step's, less what the verification alone does, and the grant of
`measuring_job`. It may, and nothing else (`tests/policy/test_pipeline.py` holds it to that):

- read the canary's secret, `rhi/canary-key`, and no other;
- query the stage's Prometheus workspace, by the tag that names its stage;
- read the release parameter of each pool the stage deploys, by its name;
- query the stage's heartbeats table, `dynamodb:Query` and no other action on that table, named by
  the verification's ARN pattern (`rhi.pipeline.verify.heartbeats_table_arn`), under the condition
  `ForAllValues:StringEquals` that `dynamodb:LeadingKeys` is the coursework pool's name alone: the
  rows the coursework pool's instances write, which the smoke test `release-heartbeat` reads, and
  no other pool's (the form of the pools' instance role, `rhi.pools.role`). The table's name is in
  the coursework pool's release parameter, so nothing lists or describes it. The same rows say
  whether an instance was launched during the wait for the session;
- read the one stack resource that names the stage's pool-state table,
  `cloudformation:DescribeStackResource` on the pools stack and on no other;
- read and write, `dynamodb:GetItem` and `dynamodb:PutItem` and no other action, the items of that
  table whose partition key is the coursework pool's name, the session the job books and stops
  among them, and no other pool's: the table is named as the hold's role names it
  (`rhi.pipeline.spikes.pool_state_table_arn`), under the same condition on `dynamodb:LeadingKeys`
  as the heartbeat rows;
- put the measurement event on the measurement bus;
- use the pipeline's connection to read the repository and write to its own log group: what any
  CodeBuild project needs to run at all.

No action of CodePipeline, Auto Scaling, EC2 or IAM, and no other action of DynamoDB or
CloudFormation, so the run makes every smoke test the verification makes on the coursework pool
after a refresh (`scripts/pipeline/canary_result.py`). The project is given the role as one that
takes no new statement (`without_policy_updates`), as the spike jobs are, so CodeBuild's construct
adds nothing to it. It runs CodeBuild's standard image and is not privileged. It keeps
no artifact, and what CodeBuild stores of a build is encrypted with the key AWS manages, the
default, as the promotion job's is: a key of its own would add a monthly cost and a key policy for
a build that holds nothing but this public repository (`tests/policy/acknowledged/pipeline_canary.toml`).
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from rhi.gateway.stack import NO_ENDPOINT
from rhi.observability.stack import STAGE_TAG
from rhi.pipeline import reporter
from rhi.pipeline.hold import pool_state_logical_id
from rhi.pipeline.spikes import CONNECTION_ACTIONS, MAX_TIMEOUT_HOURS, pool_state_table_arn
from rhi.pipeline.synth import UV_VERSION, import_path
from rhi.pipeline.verify import (
    CANARY_SECRET,
    CHECK_WAIT_MINUTES_VARIABLE,
    POOL_STATE_ITEMS,
    POOL_STATE_LOGICAL_ID_VARIABLE,
    POOLS_STACK_VARIABLE,
    SESSION_MAX_HOURS_VARIABLE,
    heartbeats_table_arn,
)
from rhi.stage import EndpointStage

# The project's and the rule's fixed name, by which the owner finds the job and its schedule.
NAME = "rhi-gpu-canary"
SCRIPT = "scripts/pipeline/canary_result.py"
# Sundays at 04:00 UTC: minute, hour, day of month, month, day of week, year.
SCHEDULE = events.Schedule.cron(minute="0", hour="4", week_day="SUN")
# The branch the job runs from: the one the pipeline deploys.
BRANCH = "main"
LOG_GROUP_NAME = f"/aws/codebuild/{NAME}"
# The one pool the canary tests: its role may query that pool's heartbeat rows alone, and read and
# write that pool's items of the pool-state table alone.
POOL = "coursework"
OF_THE_POOL = {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [POOL]}}
LOG_RETENTION = logs.RetentionDays.ONE_YEAR
# The longest the job runs beside its wait for the instance of the session it booked: the set-up,
# the smoke tests with the workspace's lag, the measurement and the stop of the session.
TIMEOUT = cdk.Duration.hours(2)

ENDPOINT_VARIABLE = "RHI_SMOKE_ENDPOINT"
RELEASE_PARAMETERS_VARIABLE = "RHI_SMOKE_RELEASE_PARAMETERS"

COMMANDS = ["uv sync --locked", f"uv run --locked python {SCRIPT}"]


def timeout(check_wait_minutes: int) -> cdk.Duration:
    """The job's timeout: `TIMEOUT` and the wait for the session's instance, `check_wait_minutes`,
    so that the build outlasts the wait, the tests and the stop of the session. A `ValueError` when
    that is longer than CodeBuild lets one build run."""
    minutes = TIMEOUT.to_minutes() + check_wait_minutes
    if minutes > MAX_TIMEOUT_HOURS * 60:
        raise ValueError(
            f"check_wait_minutes is {check_wait_minutes}: with the {TIMEOUT.to_minutes()} minutes of the "
            f"canary's other work, the job would run longer than CodeBuild's {MAX_TIMEOUT_HOURS} hours"
        )
    return cdk.Duration.minutes(minutes)


class GpuCanary(Construct):
    """The canary's job, as a project of the stack, and its weekly schedule. `connection_arn` is
    the pipeline's connection, `repository` the repository as owner/name, and `stage` the stage
    whose endpoint the job tests. While `parked`, only the job's log group: no job, no schedule."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        connection_arn: str,
        repository: str,
        stage: cdk.Stage,
        parked: bool = False,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=LOG_GROUP_NAME,
            retention=LOG_RETENTION,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.role: iam.Role | None = None
        self.project: codebuild.Project | None = None
        self.rule: events.Rule | None = None
        if parked:
            # No endpoint to test: no schedule, and no job for one to start.
            return
        # ty reads the CDK's generated principal and role as unlike their protocols: jsii passes a
        # renamed parameter by position.
        codebuild_service = iam.ServicePrincipal("codebuild.amazonaws.com")
        role = iam.Role(self, "Role", assumed_by=codebuild_service)  # ty: ignore[invalid-argument-type]
        environment: dict[str, str] = {"PYTHONPATH": ":".join(import_path())}
        for statement in self._statements(stack, stage, environment):
            role.add_to_policy(statement)
        connection_access = iam.Grant.add_to_principal(
            grantee=role, actions=CONNECTION_ACTIONS, resource_arns=[connection_arn]
        )
        reporter.measuring_job(role, environment)

        owner, _, name = repository.partition("/")
        project = codebuild.Project(
            self,
            "Project",
            project_name=NAME,
            description=f"The weekly GPU canary: {SCRIPT}, started by its schedule.",
            # A role that takes no statement: what CodeBuild's construct would add is not added.
            role=role.without_policy_updates(),
            # No webhook, and no status of CodeBuild's own: the result reaches GitHub only through
            # the reporter, in its fixed vocabulary.
            source=codebuild.Source.git_hub(
                owner=owner, repo=name, branch_or_ref=BRANCH, webhook=False, report_build_status=False
            ),
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "install": {"commands": [f"python3 -m pip install uv=={UV_VERSION}"]},
                        "build": {"commands": COMMANDS},
                    },
                }
            ),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0, privileged=False
            ),
            environment_variables={
                variable: codebuild.BuildEnvironmentVariable(value=value) for variable, value in environment.items()
            },
            logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=self.log_group)),
            # The configuration's wait, when the stage has one, and the job's other work.
            timeout=timeout(stage.config.check_wait_minutes if isinstance(stage, EndpointStage) else 0),
        )
        # The repository through the pipeline's connection, not through a credential of the account.
        resource = project.node.default_child
        assert isinstance(resource, codebuild.CfnProject)
        resource.add_property_override("Source.Auth", {"Type": "CODECONNECTIONS", "Resource": connection_arn})
        connection_access.apply_before(project)
        self.role, self.project = role, project

        # The schedule, whose one target is the job. Its role may start a build of the job alone.
        target = targets.CodeBuildProject(project)
        self.rule = events.Rule(
            self,
            "Schedule",
            rule_name=NAME,
            description="Starts the weekly GPU canary, on Sundays at 04:00 UTC.",
            schedule=SCHEDULE,
            targets=[target],  # ty: ignore[invalid-argument-type] - as above
        )

    def _statements(self, stack: cdk.Stack, stage: cdk.Stage, environment: dict[str, str]) -> list[iam.PolicyStatement]:
        """The role's permissions other than the connection and measurement grants, and what the
        job is told of `stage` in `environment`."""
        # Secrets Manager adds six random characters to a secret's name in its ARN.
        secret = stack.format_arn(
            service="secretsmanager",
            resource="secret",
            resource_name=f"{CANARY_SECRET}-??????",
            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
        )
        statements = [
            iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"], resources=[secret]),
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[self.log_group.log_group_arn]
            ),
        ]
        if not isinstance(stage, EndpointStage):
            # A stage that is not the endpoint's has no endpoint to test: the script says so, as not measured.
            environment[ENDPOINT_VARIABLE] = NO_ENDPOINT
            return statements
        endpoint = stage.gateway.endpoint.value
        assert isinstance(endpoint, str), "the gateway's endpoint is text at synthesis"
        environment[ENDPOINT_VARIABLE] = endpoint
        environment[RELEASE_PARAMETERS_VARIABLE] = json.dumps(stage.pools.release_parameters, sort_keys=True)
        # The stage's workspace, by its tag: every workspace of the account and region, narrowed to
        # the one that carries the stage's name. Its address is in the release parameter.
        workspaces = stack.format_arn(service="aps", resource="workspace", resource_name="*")
        tagged = {"StringEquals": {f"aws:ResourceTag/{STAGE_TAG}": stage.observability.stage_name}}
        statements.append(iam.PolicyStatement(actions=["aps:QueryMetrics"], resources=[workspaces], conditions=tagged))
        # The release parameter of each pool the stage deploys, by its name, and no other.
        parameters = [
            stack.format_arn(service="ssm", resource="parameter", resource_name=name.lstrip("/"))
            for name in stage.pools.release_parameters.values()
        ]
        if parameters:
            statements.append(iam.PolicyStatement(actions=["ssm:GetParameter"], resources=parameters))
        # The coursework pool's heartbeat rows: a query of the stage's heartbeats table, by the
        # verification's pattern, whose partition key may only be that pool's name.
        statements.append(
            iam.PolicyStatement(
                actions=["dynamodb:Query"], resources=[heartbeats_table_arn(stack, stage)], conditions=OF_THE_POOL
            )
        )
        # The session on the coursework pool: where the pool-state table is, as text, and the two
        # numbers of the configuration a booking is made with, under the verification's names.
        environment |= {
            POOLS_STACK_VARIABLE: stage.pools.stack_name,
            POOL_STATE_LOGICAL_ID_VARIABLE: pool_state_logical_id(stage),
            CHECK_WAIT_MINUTES_VARIABLE: str(stage.config.check_wait_minutes),
            SESSION_MAX_HOURS_VARIABLE: str(stage.config.session_max_hours),
        }
        # The table's name, by its logical ID in the pools stack: that one stack's resources.
        pools_stack = stack.format_arn(
            service="cloudformation", resource="stack", resource_name=f"{stage.pools.stack_name}/*"
        )
        statements.append(
            iam.PolicyStatement(actions=["cloudformation:DescribeStackResource"], resources=[pools_stack])
        )
        # The coursework pool's items of the pool-state table, its session among them, read and
        # written: the partition key may only be that pool's name.
        statements.append(
            iam.PolicyStatement(
                actions=POOL_STATE_ITEMS, resources=[pool_state_table_arn(stack, stage)], conditions=OF_THE_POOL
            )
        )
        return statements
