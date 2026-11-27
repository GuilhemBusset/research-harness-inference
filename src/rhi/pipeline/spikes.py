"""The spike jobs: one CodeBuild project for each entry of an explicit list (system design,
sections 8.4 and 12).

A spike that needs AWS runs as a job the owner starts: a merge never starts one, and the ones that
cost real money are the owner's call. **`JOBS` is the list.** Each entry names a job and the script
under `spikes/` it runs, and a job exists only for an entry: a module of `spikes/` that is not a
job (`spikes/common.py`, the load generator) never becomes one. Each spike's issue adds its own
entry to `JOBS`, and nothing else of this module changes. An entry may give its job a longer
timeout than the default, in hours, up to CodeBuild's limit.

**What a job is.** A CodeBuild project of the pipeline's stack, named `rhi-spike-<job>`. The owner
starts a build of it on a commit of `main`, its head unless the build is given another commit. It
reads this repository from GitHub through the pipeline's connection, and has no webhook: no
schedule, no rule and no pipeline action starts it. It runs CodeBuild's standard image, not
privileged, installs a pinned uv and the locked environment, and runs its script with the
repository's package roots on the import path (`rhi.pipeline.synth.import_path`), so a spike
imports `common` and `rhi_common` by name. The commit it runs on reaches the script as
`CODEBUILD_RESOLVED_SOURCE_VERSION`, and the measurement bus as `RHI_MEASUREMENT_BUS`.

**One role, one log group and one key, for every job.** cdk-nag acknowledges each finding by its
own id, which carries the logical ID of the resource it is on. CodeBuild's construct adds, for each
project, statements on the project's own log group and report groups to the role it is given, so a
role per job, or a shared role that each project extends, would bring a finding with every entry.
So `SpikeJobs` creates one role, one log group and one key, once, whatever the list holds, and
writes the role's statements itself. Every project is given the role as one that takes no new
statement (`without_policy_updates`), logs to the shared group, declares no report group, and is
encrypted with the shared key, which is what the AWS Solutions pack asks of a project. A new entry
then adds a project and nothing else: no statement and no finding.

**The role** may, and nothing else (`tests/policy/test_pipeline.py` holds it to that):

- read the configuration's parameters, under `/rhi/config/`, by path, as the synth step does;
- read the pool-state records, `dynamodb:GetItem` and `dynamodb:Query` on the stage's pool-state
  table, named as the verification names the heartbeats table (`rhi.pipeline.verify`);
- write, with `dynamodb:PutItem` and no other action, the items of that table whose partition key
  is the mock pool's name, and no other pool's: the condition is on `dynamodb:LeadingKeys`, in the
  form the verification's role has. A job that runs on the mock pool books that pool itself
  (section 12, `scripts/pipeline/booking.py`), and its session is one of those items;
- read the spikes' secret, `rhi/spike-keys` (`rhi_common.spike_keys`), and no other;
- query the stage's Prometheus workspace, by the tag that names its stage;
- put the measurement event on the measurement bus, through `rhi.pipeline.reporter.measuring_job`;
- use the pipeline's connection to read the repository: `codeconnections:GetConnection` and
  `codeconnections:GetConnectionToken`, what CodeBuild's user guide asks of a project's role;
- write to the shared log group, and use the shared key;
- send the document `rhi-engine-contract` (`rhi.pools.contract_check`), with `ssm:SendCommand`, on
  that document of the stage's account and region, and on the EC2 instances whose tag `rhi-pool`
  names a pool of the stage's configuration, under the condition `ssm:resourceTag/rhi-pool`: every
  instance of a pool carries the tag (`rhi.pools.group`). The document takes no parameter and runs
  only the release's own contract check, so this runs that check on a pool's instances and nothing
  else. IAM reads the condition on each resource the call names, and the document carries no such
  tag, so the right is two statements: the document alone, and the instances under the condition;
- read each invocation's status and what its step printed, `ssm:ListCommandInvocations`, which
  AWS grants only on every resource. A stage that launches no instance, parked or with no pool,
  holds neither the document nor a launch template, and its role has none of these three.

No action of Auto Scaling, EC2 or IAM: a spike reaches a pool through the gateway, as a canary
student, and changes nothing of one but the mock pool's session; the job `engine_contract` sends
that one document by the tag, and so names no instance and reads nothing of EC2 or Auto Scaling.
No job can book a GPU pool: the session of any other pool, and the session switch, are under
another partition key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aws_cdk as cdk
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from constructs import Construct

from rhi.observability.stack import STAGE_TAG
from rhi.pipeline import reporter
from rhi.pipeline.synth import UV_VERSION, config_reader, import_path
from rhi.pools.contract_check import DOCUMENT_NAME
from rhi.pools.group import POOL_TAG
from rhi.stage import EndpointStage
from rhi_common.spike_keys import SPIKE_KEYS_SECRET

if TYPE_CHECKING:
    from collections.abc import Sequence

FOLDER = "spikes"
PROJECT_PREFIX = "rhi-spike-"
_NAME = re.compile(r"[a-z0-9_]+")
_SCRIPT = re.compile(r"[a-z0-9_]+\.py")
# The branch a build runs from unless the owner gives it another commit: the one the pipeline deploys.
BRANCH = "main"
# The shared log group, under CodeBuild's prefix: what keeps Grafana from every build's logs keeps
# it from the spikes' too (`rhi.observability.grafana`).
LOG_GROUP_NAME = "/aws/codebuild/rhi-spikes"
LOG_RETENTION = logs.RetentionDays.ONE_YEAR
# Hours. A spike may wait on a pool's starts one after another, or replay a class's sessions:
# longer than CodeBuild's default hour. A job that needs more gives its own, within CodeBuild's limit.
TIMEOUT_HOURS = 8
MAX_TIMEOUT_HOURS = 36
# What CodeBuild's user guide asks of a project's role to read a repository through a connection.
CONNECTION_ACTIONS = ["codeconnections:GetConnection", "codeconnections:GetConnectionToken"]
# What CodeBuild does with a project's key: encrypt and decrypt what it stores of a build.
KEY_ACTIONS = ["kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:GenerateDataKey"]
POOL_STATE_READS = ["dynamodb:GetItem", "dynamodb:Query"]
# The pool a job may book, by its name: the partition key of the only items of the pool-state table
# the role may write, with this one action.
MOCK_POOL = "mock"
POOL_STATE_WRITES = ["dynamodb:PutItem"]
# What sends the contract check to a pool's instances, and reads what each invocation printed.
CONTRACT_SENDS = ["ssm:SendCommand"]
CONTRACT_READS = ["ssm:ListCommandInvocations"]


@dataclass(frozen=True)
class SpikeJob:
    """One spike job: its name, lower-case letters, digits and underscores, which is also the name
    `spikes/common.py` gives its runs, the script it runs, a file directly under `spikes/`, and the
    hours a build of it may take."""

    name: str
    script: str
    timeout_hours: int = TIMEOUT_HOURS

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"a spike job's name is lower-case letters, digits and underscores: {self.name!r}")
        if not _SCRIPT.fullmatch(self.script):
            raise ValueError(f"a spike job's script is a Python file directly under {FOLDER}/: {self.script!r}")
        if not 1 <= self.timeout_hours <= MAX_TIMEOUT_HOURS:
            raise ValueError(f"a spike job's timeout is 1 to {MAX_TIMEOUT_HOURS} hours: {self.timeout_hours!r}")

    @property
    def project_name(self) -> str:
        """The project's fixed name, by which the owner starts a build of it."""
        return f"{PROJECT_PREFIX}{self.name}"

    @property
    def path(self) -> str:
        """The script's path in the repository."""
        return f"{FOLDER}/{self.script}"


# The spike jobs. Each spike's issue adds its entry here, and only that line.
JOBS: tuple[SpikeJob, ...] = (
    SpikeJob("s8_stickiness", "s8_stickiness.py"),
    SpikeJob("s7_drain", "s7_drain.py"),
    SpikeJob("s5_capacity", "s5_capacity.py"),
    SpikeJob("s3_harness_task", "s3_harness_task.py"),
    SpikeJob("s3_second_session", "s3_second_session.py"),
    SpikeJob("dry_run_class", "dry_run_class.py"),
    SpikeJob("engine_contract", "engine_contract.py"),
)


def commands(job: SpikeJob) -> list[str]:
    """The build's commands: the locked environment, then the job's script."""
    return ["uv sync --locked", f"uv run --locked python {job.path}"]


def pool_state_table_arn(scope: cdk.Stack, stage: EndpointStage) -> str:
    """The ARN pattern of the pool-state table of `stage`, in the partition, region and account of
    `scope`: the name CloudFormation gives it, the pools stack's name, a hyphen, the table's logical
    ID, a hyphen and the suffix it generates. The logical ID is read as text: a reference to it
    would be one between two assemblies."""
    pools = stage.pools
    table = pools.tables.pool_state.node.default_child
    assert isinstance(table, cdk.CfnElement)
    logical_id = pools.resolve(table.logical_id)
    assert isinstance(logical_id, str)
    return scope.format_arn(service="dynamodb", resource="table", resource_name=f"{pools.stack_name}-{logical_id}-*")


class SpikeJobs(Construct):
    """The spike jobs of `jobs`, each as a project of `projects`, on the shared `role`, `log_group`
    and `key`. `connection_arn` is the pipeline's connection, `repository` the repository as
    owner/name, and `stage` the stage whose workspace and pool-state table the jobs read."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        jobs: Sequence[SpikeJob],
        connection_arn: str,
        repository: str,
        stage: cdk.Stage,
    ) -> None:
        super().__init__(scope, construct_id)
        names = [job.name for job in jobs]
        if len(set(names)) != len(names):
            raise ValueError("two spike jobs have one name")
        stack = cdk.Stack.of(self)
        self.key = kms.Key(self, "Key", enable_key_rotation=True, removal_policy=cdk.RemovalPolicy.DESTROY)
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
        for statement in self._statements(stack, stage):
            self.role.add_to_policy(statement)
        connection_access = iam.Grant.add_to_principal(
            grantee=self.role, actions=CONNECTION_ACTIONS, resource_arns=[connection_arn]
        )
        environment: dict[str, str] = {"PYTHONPATH": ":".join(import_path())}
        reporter.measuring_job(self.role, environment)

        # Every project gets the role as one that takes no statement: what CodeBuild's construct
        # would add for each project, its own log group and report groups, is not added.
        shared = self.role.without_policy_updates()
        owner, _, name = repository.partition("/")
        self.projects: dict[str, codebuild.Project] = {}
        for job in jobs:
            project = codebuild.Project(
                self,
                job.name,
                project_name=job.project_name,
                description=f"The spike job {job.name}: {job.path}, started by the owner.",
                role=shared,
                # No webhook, and no status of CodeBuild's own on the commit: a job's numbers reach
                # GitHub only through the reporter, in its fixed vocabulary.
                source=codebuild.Source.git_hub(
                    owner=owner, repo=name, branch_or_ref=BRANCH, webhook=False, report_build_status=False
                ),
                build_spec=codebuild.BuildSpec.from_object(
                    {
                        "version": "0.2",
                        "phases": {
                            "install": {"commands": [f"python3 -m pip install uv=={UV_VERSION}"]},
                            "build": {"commands": commands(job)},
                        },
                    }
                ),
                environment=codebuild.BuildEnvironment(
                    build_image=codebuild.LinuxBuildImage.STANDARD_7_0, privileged=False
                ),
                environment_variables={
                    variable: codebuild.BuildEnvironmentVariable(value=value) for variable, value in environment.items()
                },
                encryption_key=self.key,  # ty: ignore[invalid-argument-type] - as above
                logging=codebuild.LoggingOptions(
                    cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=self.log_group)
                ),
                timeout=cdk.Duration.hours(job.timeout_hours),
            )
            # The repository through the pipeline's connection, not through a credential of the account.
            resource = project.node.default_child
            assert isinstance(resource, codebuild.CfnProject)
            resource.add_property_override("Source.Auth", {"Type": "CODECONNECTIONS", "Resource": connection_arn})
            connection_access.apply_before(project)
            self.projects[job.name] = project

    def _statements(self, stack: cdk.Stack, stage: cdk.Stage) -> list[iam.PolicyStatement]:
        """The shared role's permissions other than the connection and measurement grants."""
        # Secrets Manager adds six random characters to a secret's name in its ARN.
        secret = stack.format_arn(
            service="secretsmanager",
            resource="secret",
            resource_name=f"{SPIKE_KEYS_SECRET}-??????",
            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
        )
        statements = [
            config_reader(stack),
            iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"], resources=[secret]),
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[self.log_group.log_group_arn]
            ),
            iam.PolicyStatement(actions=KEY_ACTIONS, resources=[self.key.key_arn]),
        ]
        if isinstance(stage, EndpointStage):
            # The stage's workspace, by its tag: every workspace of the account and region,
            # narrowed to the one that carries the stage's name.
            workspaces = stack.format_arn(service="aps", resource="workspace", resource_name="*")
            tagged = {"StringEquals": {f"aws:ResourceTag/{STAGE_TAG}": stage.observability.stage_name}}
            statements.append(
                iam.PolicyStatement(actions=["aps:QueryMetrics"], resources=[workspaces], conditions=tagged)
            )
            pool_state = pool_state_table_arn(stack, stage)
            statements.append(iam.PolicyStatement(actions=POOL_STATE_READS, resources=[pool_state]))
            # The mock pool's items of that table, its session among them: the partition key of a
            # write may only be that pool's name.
            statements.append(
                iam.PolicyStatement(
                    actions=POOL_STATE_WRITES,
                    resources=[pool_state],
                    conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [MOCK_POOL]}},
                )
            )
            statements += contract_check(stack, stage)
        return statements


def contract_check(stack: cdk.Stack, stage: EndpointStage) -> list[iam.PolicyStatement]:
    """What sends the contract check: the one document, and the instances of the stage's pools by
    their tag, with the condition on a statement of its own since the document carries no such
    tag; and the read of the invocations. None for a stage that launches no instance, parked or
    with no pool: its pools stack holds neither the document nor a launch template."""
    pools = sorted(stage.pools.groups)
    if stage.pools.contract_check is None or not pools:
        return []
    document = stack.format_arn(service="ssm", resource="document", resource_name=DOCUMENT_NAME)
    instances = stack.format_arn(service="ec2", resource="instance", resource_name="*")
    return [
        iam.PolicyStatement(actions=CONTRACT_SENDS, resources=[document]),
        iam.PolicyStatement(
            actions=CONTRACT_SENDS,
            resources=[instances],
            conditions={"StringEquals": {f"ssm:resourceTag/{POOL_TAG}": pools}},
        ),
        # Run Command grants listing invocations only on every resource.
        iam.PolicyStatement(actions=CONTRACT_READS, resources=["*"]),
    ]
