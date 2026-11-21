"""The weekly GPU canary: a job that runs the coursework pool's smoke tests against the deployed
endpoint, and the schedule that starts it (system design, sections 8.2, step 6, 11.2 and 14).

**What it starts.** One EventBridge rule, `rhi-gpu-canary`, on a schedule, whose one target is the
canary's CodeBuild project, `rhi-gpu-canary`. The rule's role may start a build of that project
and nothing else. The job runs `scripts/pipeline/canary_result.py` on the head of `main`: the
smoke tests of `tests/smoke` against the deployed endpoint, on the coursework pool, with the
canary's key, then the measurement `gpu-canary` of their result, sent to the reporter through the
measurement bus. It starts no execution of the pipeline and deploys nothing: it tests what is
deployed, whatever was deployed, and the job has no webhook.

**When.** Once a week, on Sundays at 04:00 UTC, `SCHEDULE`: an EventBridge rule reads its cron
expression in UTC. Section 8.5 holds deployments outside session windows; the canary is not a
deployment, so the hold does not apply to it. Neither the configuration nor the design gives a
time: this module fixes it.

**What the job is told**, as text built at synthesis, since this stack holds no reference to a
resource of the stage:

- `RHI_SMOKE_ENDPOINT`: the endpoint's address, the value of the gateway stack's output, which
  reads `none` while the endpoint has no HTTPS listener;
- `RHI_SMOKE_RELEASE_PARAMETERS`: each pool's release parameter, as JSON from the pool's name to
  the parameter's name, which the smoke tests read, and whose settings, for the coursework pool,
  give the script the stage's Prometheus workspace when it runs;
- `RHI_MEASUREMENT_BUS`: the measurement bus, through `rhi.pipeline.reporter.measuring_job`;
- `PYTHONPATH`: the repository's package roots, `rhi.pipeline.synth.import_path`.

**The role** is the verification step's, less what the verification alone does, and the grant of
`measuring_job`. It may, and nothing else (`tests/policy/test_pipeline.py` holds it to that):

- read the canary's secret, `rhi/canary-key`, and no other;
- query the stage's Prometheus workspace, by the tag that names its stage;
- read the release parameter of each pool the stage deploys, by its name;
- put the measurement event on the measurement bus;
- use the pipeline's connection to read the repository and write to its own log group: what any
  CodeBuild project needs to run at all.

No action of CodePipeline, DynamoDB, Auto Scaling, EC2 or IAM: the verification's query of the
heartbeats table is not the canary's, so its run leaves out the one smoke test that reads that table
(`scripts/pipeline/canary_result.py`). The project is given the role as one that takes no new
statement (`without_policy_updates`), as the spike jobs are, so CodeBuild's construct adds nothing
to it. It runs CodeBuild's standard image and is not privileged. It keeps
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
from rhi.pipeline.spikes import CONNECTION_ACTIONS
from rhi.pipeline.synth import UV_VERSION, import_path
from rhi.pipeline.verify import CANARY_SECRET
from rhi.stage import EndpointStage

# The project's and the rule's fixed name, by which the owner finds the job and its schedule.
NAME = "rhi-gpu-canary"
SCRIPT = "scripts/pipeline/canary_result.py"
# Sundays at 04:00 UTC: minute, hour, day of month, month, day of week, year.
SCHEDULE = events.Schedule.cron(minute="0", hour="4", week_day="SUN")
# The branch the job runs from: the one the pipeline deploys.
BRANCH = "main"
LOG_GROUP_NAME = f"/aws/codebuild/{NAME}"
LOG_RETENTION = logs.RetentionDays.ONE_YEAR
# A cold wake of the coursework pool, the hold, the smoke tests and the workspace's lag.
TIMEOUT = cdk.Duration.hours(2)

ENDPOINT_VARIABLE = "RHI_SMOKE_ENDPOINT"
RELEASE_PARAMETERS_VARIABLE = "RHI_SMOKE_RELEASE_PARAMETERS"

COMMANDS = ["uv sync --locked", f"uv run --locked python {SCRIPT}"]


class GpuCanary(Construct):
    """The canary's job, as a project of the stack, and its weekly schedule. `connection_arn` is
    the pipeline's connection, `repository` the repository as owner/name, and `stage` the stage
    whose endpoint the job tests."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        connection_arn: str,
        repository: str,
        stage: cdk.Stage,
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
        # ty reads the CDK's generated principal and role as unlike their protocols: jsii passes a
        # renamed parameter by position.
        codebuild_service = iam.ServicePrincipal("codebuild.amazonaws.com")
        self.role = iam.Role(self, "Role", assumed_by=codebuild_service)  # ty: ignore[invalid-argument-type]
        environment: dict[str, str] = {"PYTHONPATH": ":".join(import_path())}
        for statement in self._statements(stack, stage, environment):
            self.role.add_to_policy(statement)
        connection_access = iam.Grant.add_to_principal(
            grantee=self.role, actions=CONNECTION_ACTIONS, resource_arns=[connection_arn]
        )
        reporter.measuring_job(self.role, environment)

        owner, _, name = repository.partition("/")
        self.project = codebuild.Project(
            self,
            "Project",
            project_name=NAME,
            description=f"The weekly GPU canary: {SCRIPT}, started by its schedule.",
            # A role that takes no statement: what CodeBuild's construct would add is not added.
            role=self.role.without_policy_updates(),
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
            timeout=TIMEOUT,
        )
        # The repository through the pipeline's connection, not through a credential of the account.
        resource = self.project.node.default_child
        assert isinstance(resource, codebuild.CfnProject)
        resource.add_property_override("Source.Auth", {"Type": "CODECONNECTIONS", "Resource": connection_arn})
        connection_access.apply_before(self.project)

        # The schedule, whose one target is the job. Its role may start a build of the job alone.
        target = targets.CodeBuildProject(self.project)
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
        return statements
