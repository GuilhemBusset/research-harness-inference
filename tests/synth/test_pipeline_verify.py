"""The pipeline's verification step (src/rhi/pipeline/verify.py; system design, section 8.2, step
6): the action named Verify, after the stage's deployment, which runs the decision whether the
execution verifies on the coursework pool before the verification script, and builds no image; a post-build phase that copies the script's reason, when there is one, to the
key verification/<execution ID>/not-run of the artifact bucket, the IDs of the smoke tests
that failed to failed-smoke-tests beside it, the last lifecycle stage each instance reached to
last-stages, and the reason the GPU tests did not run to gpu-not-run; inputs that are outputs of the
stage's stacks and the execution's ID; and a role that
reads the canary's secret, queries the stage's workspace, reads the stage's pools' release
parameters, queries the stage's heartbeats table, and that one table, writes under
verification/, and lists its own pipeline's executions, with no action of Auto Scaling. For
the sessions the script books, on the mock pool and on the coursework pool, the step is given the
pools stack's name, the pool-state table's logical ID and the configuration's two numbers, its role
may describe that one stack's resources and read and write the items of those two pools of that one
table, and its timeout grows with the wait for each of them.

The tests read the app's own pipeline, the one that is deployed. Two of them also build the app
from a changed configuration, with no listener and with no pool.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import gpu_verification as decision
import verify as script
from rhi import config

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

Templates = dict[str, dict[str, Any]]

REPO = Path(__file__).resolve().parents[2]
PIPELINE_TEMPLATE = "RhiPipeline.template.json"
STAGE = "Rhi"
ACTION = "Verify"
CLOUDFORMATION = "CloudFormation"
EXECUTION_ID = "#{codepipeline.PipelineExecutionId}"
# An output of a deployed stack, as CodePipeline's variable of the deployment action's namespace.
STACK_OUTPUT = re.compile(r"#\{(\w+)\.(\w+)\}")
# The tag the stage's workspace is named by.
STAGE_TAG = "rhi-stage"
RELEASE_PARAMETER = re.compile(r"/rhi/pools/[a-z0-9-]+/release")


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def text(value: Any) -> str:
    """A value of the template as text, `<>` in place of each pseudo parameter and `<Name>` or
    `<Name.Attribute>` in place of each resource."""
    if isinstance(value, str):
        return value
    ((function, argument),) = value.items()
    if function == "Fn::Join":
        return argument[0].join(text(part) for part in argument[1])
    if function == "Ref":
        return "<>" if argument.startswith("AWS::") else f"<{argument}>"
    assert function == "Fn::GetAtt", value
    return f"<{'.'.join(argument)}>"


class Verification:
    """The verification of a synthesized pipeline: its action, its project and its role."""

    def __init__(self, templates: Templates) -> None:
        self.templates = templates
        self.resources = templates[PIPELINE_TEMPLATE]["Resources"]
        (pipeline,) = [r["Properties"] for r in self.resources.values() if r["Type"] == "AWS::CodePipeline::Pipeline"]
        self.pipeline = pipeline
        # One action of that name in the whole pipeline, in the stage it verifies.
        ((stage, self.action),) = [
            (stage, action) for stage in pipeline["Stages"] for action in stage["Actions"] if action["Name"] == ACTION
        ]
        assert stage["Name"] == STAGE
        self.actions = stage["Actions"]
        self.project = self.resources[self.action["Configuration"]["ProjectName"]["Ref"]]["Properties"]
        self.phases = json.loads(self.project["Source"]["BuildSpec"])["phases"]
        role = self.project["ServiceRole"]["Fn::GetAtt"][0]
        assert not self.resources[role]["Properties"].get("ManagedPolicyArns")
        self.statements = [
            statement
            for policy in self.resources.values()
            if policy["Type"] == "AWS::IAM::Policy" and {"Ref": role} in policy["Properties"]["Roles"]
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        ]

    def deployments(self) -> list[dict[str, Any]]:
        return [action for action in self.actions if action["ActionTypeId"]["Provider"] == CLOUDFORMATION]

    def given_by_the_action(self) -> dict[str, str]:
        """The variables the action gives the build, which CodePipeline resolves in each execution.
        They are text: the pipeline's template holds a variable's name, never a value of the stage."""
        variables = json.loads(self.action["Configuration"]["EnvironmentVariables"])
        return {entry["name"]: entry["value"] for entry in variables}

    def given_by_the_project(self) -> dict[str, Any]:
        return {entry["Name"]: entry["Value"] for entry in self.project["Environment"]["EnvironmentVariables"]}

    def stacks(self) -> dict[str, dict[str, Any]]:
        """The template of each stack the stage's actions deploy, by the name they deploy it under."""
        return {
            a["Configuration"]["StackName"]: self.templates[a["Configuration"]["TemplatePath"].partition("::")[2]]
            for a in self.deployments()
            if "TemplatePath" in a["Configuration"]
        }

    def output(self, variable: str) -> tuple[str, Any, dict[str, Any]]:
        """The stack output a variable of the action reads: the stack's name as the pipeline
        deploys it, the output's value and the template that declares it."""
        match = STACK_OUTPUT.fullmatch(self.given_by_the_action()[variable])
        assert match is not None, f"{variable} is not an output of a deployed stack"
        # The namespace is that of the action that executes the stack's change set; the action that
        # prepares the change set names the template.
        (stack,) = [a["Configuration"]["StackName"] for a in self.deployments() if a.get("Namespace") == match[1]]
        (path,) = [
            a["Configuration"]["TemplatePath"].partition("::")[2]
            for a in self.deployments()
            if a["Configuration"]["StackName"] == stack and "TemplatePath" in a["Configuration"]
        ]
        return stack, self.templates[path]["Outputs"][match[2]]["Value"], self.templates[path]

    def allowed(self, service: str) -> list[dict[str, Any]]:
        """The statements of the role with an action of one service."""
        return [s for s in self.statements if any(a.startswith(f"{service}:") for a in as_list(s["Action"]))]


def fake_aws(folder: Path) -> Path:
    """A folder that holds the AWS CLI, as far as the post-build phase uses it: `aws s3 cp <file>
    s3://<bucket>/<key>` copies into the folder `$BUCKETS`, and fails while `$REFUSES` is set."""
    tools = folder / "tools"
    tools.mkdir()
    (tools / "aws").write_text(
        '#!/bin/sh\n[ "$#" = 4 ] && [ "$1 $2" = "s3 cp" ] || exit 64\n[ -z "$REFUSES" ] || exit 1\n'
        'to="$BUCKETS/${4#s3://}"\nmkdir -p "$(dirname "$to")" && cp "$3" "$to"\n',
        encoding="utf-8",
    )
    (tools / "aws").chmod(0o755)
    return tools


@pytest.fixture(scope="module")
def verification(templates: Templates) -> Verification:
    """The verification of the app's pipeline."""
    return Verification(templates)


def test_the_verification_is_the_action_named_verify_after_the_deployment(verification: Verification) -> None:
    assert verification.action["ActionTypeId"] == {
        "Category": "Build",
        "Owner": "AWS",
        "Provider": "CodeBuild",
        "Version": "1",
    }
    deployed_at = [action["RunOrder"] for action in verification.deployments()]
    assert deployed_at and verification.action["RunOrder"] > max(deployed_at)
    # It runs on the source: the tests of the commit that was deployed.
    (source,) = verification.pipeline["Stages"][0]["Actions"]
    assert verification.action["InputArtifacts"] == source["OutputArtifacts"]


def test_the_step_runs_the_script_outside_the_network_and_builds_no_image(verification: Verification) -> None:
    install, build = verification.phases["install"]["commands"], verification.phases["build"]["commands"]
    assert all(re.fullmatch(r"python3 -m pip install uv==\d+\.\d+\.\d+", command) for command in install) and install
    # The decision and the script are each a command of their own, after the locked environment:
    # each one's exit is the build's.
    assert build == [
        "uv sync --locked",
        "uv run --locked python scripts/pipeline/gpu_verification.py",
        "uv run --locked python scripts/pipeline/verify.py",
    ]
    assert (REPO / "scripts/pipeline/verify.py").is_file() and (REPO / "scripts/pipeline/gpu_verification.py").is_file()
    assert "VpcConfig" not in verification.project
    assert verification.project["Environment"]["PrivilegedMode"] is False
    commands = [command for phase in verification.phases.values() for command in phase["commands"]]
    assert not any("docker" in command for command in commands)


def test_the_post_build_phase_copies_what_the_script_left(verification: Verification, tmp_path: Path) -> None:
    by_action, by_project = verification.given_by_the_action(), verification.given_by_the_project()
    (execution,) = [name for name, value in by_action.items() if value == EXECUTION_ID]
    (store,) = [verification.pipeline["ArtifactStore"]["Location"]]
    (bucket,) = [name for name, value in by_project.items() if value == store]
    reason_file, failed_file = script.REASON_FILE_VARIABLE, script.FAILED_TESTS_FILE_VARIABLE
    stages_file, gpu_file = script.LAST_STAGES_FILE_VARIABLE, script.GPU_REASON_FILE_VARIABLE
    files = (reason_file, failed_file, stages_file, gpu_file)
    # The files the step names are four, outside the checkout, which CodeBuild places under its own folder.
    assert len({by_project[file] for file in files}) == 4
    for file in files:
        assert Path(by_project[file]).is_absolute() and not by_project[file].startswith("/codebuild")

    tools, buckets = fake_aws(tmp_path), tmp_path / "buckets"

    def post_build(
        reason: str | None,
        *,
        failed: str | None = None,
        stages: str | None = None,
        gpu: str | None = None,
        refuses: bool = False,
    ) -> tuple[int, dict[str, str]]:
        """Runs the phase for the execution `an-execution`, after a script that left `reason`,
        the failed tests `failed`, the instances' last stages `stages` and `gpu` as the reason its
        GPU tests did not run: its exit, and the objects of the buckets."""
        shutil.rmtree(buckets, ignore_errors=True)
        folder = tmp_path / "left"
        folder.mkdir(exist_ok=True)
        for name, held in (("reason", reason), ("failed", failed), ("stages", stages), ("gpu", gpu)):
            (folder / name).unlink(missing_ok=True)
            if held is not None:
                (folder / name).write_text(held, encoding="utf-8")
        environ = {
            "PATH": f"{tools}:/usr/bin:/bin",
            "BUCKETS": str(buckets),
            "REFUSES": "yes" if refuses else "",
            # CodeBuild's word of a build whose build phase failed: the phase copies what there is all the same.
            "CODEBUILD_BUILD_SUCCEEDING": "0",
            reason_file: str(folder / "reason"),
            failed_file: str(folder / "failed"),
            stages_file: str(folder / "stages"),
            gpu_file: str(folder / "gpu"),
            bucket: "artifacts",
            execution: "an-execution",
        }
        exits = [
            subprocess.run(["/bin/sh", "-c", command], env=environ, check=False).returncode
            for command in verification.phases["post_build"]["commands"]
        ]
        objects = {
            path.relative_to(buckets).as_posix(): path.read_text(encoding="utf-8")
            for path in buckets.rglob("*")
            if path.is_file()
        }
        return max(exits), objects

    # Neither file: every test ran and passed, and nothing is written.
    assert post_build(None) == (0, {})
    # A copy that fails fails the build: a verification that was not run is not taken for one that passed.
    assert post_build("canary-missing\n", refuses=True)[0] != 0
    assert post_build("listener-off\n") == (0, {"artifacts/verification/an-execution/not-run": "listener-off\n"})
    # The IDs of the tests that failed, as the script left them, beside where the reason goes. The
    # phase is the build's post-build phase, which CodeBuild runs after a build phase that failed.
    assert post_build(None, failed="hello\nno-key\n") == (
        0,
        {"artifacts/verification/an-execution/failed-smoke-tests": "hello\nno-key\n"},
    )
    # And, after tests that failed, the last stage each instance reached, as the script left them.
    failed = {
        "artifacts/verification/an-execution/failed-smoke-tests": "hello\n",
        "artifacts/verification/an-execution/last-stages": "weights\nboot\n",
    }
    assert post_build(None, failed="hello\n", stages="weights\nboot\n") == (0, failed)
    # And the reason the GPU tests did not run, to the mark gpu-not-run, which is not the mark of a
    # verification that ran no test. A copy of it that fails fails the build too: such a
    # verification is not taken for one whose GPU tests ran.
    assert post_build(None, gpu="no-capacity\n") == (
        0,
        {"artifacts/verification/an-execution/gpu-not-run": "no-capacity\n"},
    )
    assert post_build(None, gpu="no-capacity\n", refuses=True)[0] != 0
    # The reason is copied when the mock pool's tests failed too, beside what the failure left: it
    # says what this build did on the pool. The reporter tells the mark of an attempt that failed
    # from that of the retry that passes (`tests/unit/reporter/test_handler.py`).
    assert post_build(None, failed="hello\n", stages="weights\nboot\n", gpu="no-capacity\n") == (
        0,
        failed | {"artifacts/verification/an-execution/gpu-not-run": "no-capacity\n"},
    )


def test_the_inputs_are_outputs_of_the_stage_s_stacks_and_the_execution_s_id(verification: Verification) -> None:
    by_action = verification.given_by_the_action()
    (execution,) = [name for name, value in by_action.items() if value == EXECUTION_ID]
    assert set(by_action) == {
        execution,
        script.ENDPOINT_VARIABLE,
        "RHI_SMOKE_WORKSPACE_URL",
        script.RELEASE_PARAMETERS_VARIABLE,
        script.HEARTBEATS_TABLE_VARIABLE,
    }

    # The endpoint is the output of the stack that holds the gateway's service, which the pipeline
    # deploys: the example configuration has a listener, so the output is its domain.
    stack, value, template = verification.output(script.ENDPOINT_VARIABLE)
    assert stack == f"{STAGE}-Gateway"
    assert [r for r in template["Resources"].values() if r["Type"] == "AWS::ECS::Service"]
    example = config.load({"RHI_CONFIG": "example"})
    assert example.https_listener and value == example.domain
    assert script.ENDPOINT_VARIABLE not in verification.given_by_the_project()

    stack, value, template = verification.output("RHI_SMOKE_WORKSPACE_URL")
    assert stack == f"{STAGE}-Observability"
    (workspace,) = [name for name, r in template["Resources"].items() if r["Type"] == "AWS::APS::Workspace"]
    assert value == {"Fn::GetAtt": [workspace, "PrometheusEndpoint"]}

    stack, value, template = verification.output(script.RELEASE_PARAMETERS_VARIABLE)
    assert stack == f"{STAGE}-Pools"
    # The script reads the output as each pool's name with its parameter's, and the stack holds
    # each parameter under that name.
    named = script.release_parameters(value)
    declared = {
        r["Properties"]["Name"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::SSM::Parameter" and RELEASE_PARAMETER.fullmatch(str(r["Properties"].get("Name")))
    }
    assert set(named) == {pool.name for pool in example.pools} and named
    assert set(named.values()) == declared
    # CodePipeline writes an output's value into the JSON of the action's configuration as it is.
    assert '"' not in value and "\\" not in value

    # The heartbeats table's name, which the deployment generates: the pools stack's output of the
    # table whose rows are kept by pool and by source, as the script reads them.
    stack, value, template = verification.output(script.HEARTBEATS_TABLE_VARIABLE)
    assert stack == f"{STAGE}-Pools"
    table = template["Resources"][value["Ref"]]
    assert table["Type"] == "AWS::DynamoDB::Table"
    assert [key["AttributeName"] for key in table["Properties"]["KeySchema"]] == [
        script.POOL_ATTRIBUTE,
        script.SOURCE_ATTRIBUTE,
    ]


def test_the_role_reads_one_secret_and_writes_under_verification_only(verification: Verification) -> None:
    (secret,) = verification.allowed("secretsmanager")
    assert secret["Action"] == "secretsmanager:GetSecretValue"
    assert text(secret["Resource"]) == f"arn:<>:secretsmanager:<>:<>:secret:{script.CANARY_SECRET}-??????"

    store = verification.pipeline["ArtifactStore"]["Location"]["Ref"]
    writes = [
        statement
        for statement in verification.allowed("s3")
        if not all(re.fullmatch(r"s3:(Get|List)\w*\*?", action) for action in as_list(statement["Action"]))
    ]
    (write,) = writes
    assert write["Action"] == "s3:PutObject"
    assert text(write["Resource"]) == f"<{store}.Arn>/verification/*"

    # Beside its grants, only CodeBuild's own: the project's logs and test reports.
    services = {action.partition(":")[0] for s in verification.statements for action in as_list(s["Action"])}
    assert services == {
        "secretsmanager",
        "s3",
        "aps",
        "ssm",
        "dynamodb",
        "cloudformation",
        "codepipeline",
        "logs",
        "codebuild",
    }
    assert all(statement["Effect"] == "Allow" for statement in verification.statements)


def test_the_role_queries_the_stage_s_workspace_and_reads_the_stage_s_release_parameters(
    verification: Verification,
) -> None:
    (query,) = verification.allowed("aps")
    assert query["Action"] == "aps:QueryMetrics"
    assert text(query["Resource"]) == "arn:<>:aps:<>:<>:workspace/*"
    # Of those workspaces, the one that carries the stage's name: the workspace whose address the
    # step is given, and no other workspace of any template.
    ((condition, stage),) = query["Condition"]["StringEquals"].items()
    assert list(query["Condition"]) == ["StringEquals"]
    assert condition == f"aws:ResourceTag/{STAGE_TAG}" and stage == STAGE
    _, address, _ = verification.output("RHI_SMOKE_WORKSPACE_URL")
    tagged = [
        name
        for template in verification.templates.values()
        for name, r in template["Resources"].items()
        if r["Type"] == "AWS::APS::Workspace" and {"Key": STAGE_TAG, "Value": stage} in r["Properties"].get("Tags", [])
    ]
    assert tagged == [address["Fn::GetAtt"][0]]

    # Each parameter the step is given, by its name, and no pattern: one action of SSM, which
    # also reads the earlier version the decision compares a release with.
    (read,) = verification.allowed("ssm")
    assert read["Action"] == "ssm:GetParameter"
    _, value, _ = verification.output(script.RELEASE_PARAMETERS_VARIABLE)
    given = script.release_parameters(value)
    assert sorted(text(r) for r in as_list(read["Resource"])) == sorted(
        f"arn:<>:ssm:<>:<>:parameter{name}" for name in given.values()
    )


def test_the_decision_on_the_coursework_pool_ends_before_the_coursework_tests(
    verification: Verification, tmp_path: Path
) -> None:
    # The decision runs in the build phase, after the locked environment and before the script that
    # runs the smoke tests on the coursework pool: a command that fails ends the phase, so a failed
    # decision runs no test.
    build = verification.phases["build"]["commands"]
    (decides,) = [at for at, command in enumerate(build) if "scripts/pipeline/gpu_verification.py" in command]
    (tests,) = [at for at, command in enumerate(build) if "scripts/pipeline/verify.py" in command]
    assert build.index("uv sync --locked") < decides < tests
    # The two share one file, outside the checkout: the pools the decision left, which the script reads.
    by_project, by_action = verification.given_by_the_project(), verification.given_by_the_action()
    left = by_project[decision.GPU_POOLS_FILE_VARIABLE]
    assert script.GPU_POOLS_FILE_VARIABLE == decision.GPU_POOLS_FILE_VARIABLE
    assert Path(left).is_absolute() and not left.startswith("/codebuild")
    # The decision is given the execution's ID and reads the pipeline's name from CodeBuild: the
    # project's environment holds no reference to the pipeline, which refers to the project.
    assert by_action[decision.EXECUTION_VARIABLE] == EXECUTION_ID
    (pipeline,) = [k for k, r in verification.resources.items() if r["Type"] == "AWS::CodePipeline::Pipeline"]
    assert pipeline not in json.dumps(verification.project)
    # It reads the release parameters the script reads, under the same variable, and may list the
    # executions of this pipeline, by its ARN. It asks nothing of Auto Scaling: no refresh is
    # started, so the role may describe neither a group nor a refresh.
    assert decision.RELEASE_PARAMETERS_VARIABLE == script.RELEASE_PARAMETERS_VARIABLE
    assert verification.allowed("autoscaling") == []
    (lists,) = verification.allowed("codepipeline")
    assert lists["Action"] == "codepipeline:ListPipelineExecutions"
    assert text(lists["Resource"]) == f"arn:<>:codepipeline:<>:<>:<{pipeline}>"


def test_the_heartbeat_grant_names_one_table(verification: Verification) -> None:
    # One statement on DynamoDB that queries: it reads a table's items by their partition key, the pool.
    (query,) = [s for s in verification.allowed("dynamodb") if "dynamodb:Query" in as_list(s["Action"])]
    assert query["Action"] == "dynamodb:Query"
    (resource,) = as_list(query["Resource"])

    # The table the step is given the name of, under its logical ID in the synthesized pools
    # template: the role names it as CloudFormation names a table that has no name of its own, the
    # stack's name, a hyphen, the logical ID, a hyphen and, for the generated suffix, a wildcard,
    # in the partition, region and account of the pipeline's own stack.
    stack, value, template = verification.output(script.HEARTBEATS_TABLE_VARIABLE)
    heartbeats = value["Ref"]
    assert stack == f"{STAGE}-Pools" and template is verification.stacks()[stack]
    assert "TableName" not in template["Resources"][heartbeats]["Properties"]
    assert text(resource) == f"arn:<>:dynamodb:<>:<>:table/{stack}-{heartbeats}-*"
    assert resource["Fn::Join"][1][1::2] == [
        {"Ref": "AWS::Partition"},
        {"Ref": "AWS::Region"},
        {"Ref": "AWS::AccountId"},
    ]

    def matches(stack: str, logical_id: str) -> bool:
        """Whether the role's pattern matches the name CloudFormation would give that table."""
        name, pattern = f"{stack}-{logical_id}-1A2B3C4D5E6F7", text(resource).partition(":table/")[2]
        return fnmatch.fnmatchcase(name, pattern)

    # A logical ID holds no hyphen, so the pattern ends where the table's own name does: it matches
    # the heartbeats table, and no other table of any template the stage deploys.
    tables = [
        (name, logical_id)
        for name, deployed in verification.stacks().items()
        for logical_id, held in deployed["Resources"].items()
        if held["Type"].startswith("AWS::DynamoDB::")
    ]
    assert all("-" not in logical_id for _, logical_id in tables)
    assert [table for table in tables if matches(*table)] == [(stack, heartbeats)]
    assert {name for name, _ in tables} > {stack} and len([name for name, _ in tables if name == stack]) > 1


def test_the_step_is_given_what_the_booking_of_its_two_pools_needs(verification: Verification) -> None:
    example, given = config.load({"RHI_CONFIG": "example"}), verification.given_by_the_project()
    # The pools stack and the pool-state table's logical ID in it, as text: the stack the pipeline
    # deploys under that name holds a table of that logical ID, keyed by pool and by record.
    stack, logical_id = given[script.POOLS_STACK_VARIABLE], given[script.POOL_STATE_LOGICAL_ID_VARIABLE]
    assert stack == f"{STAGE}-Pools" and isinstance(logical_id, str)
    table = verification.stacks()[stack]["Resources"][logical_id]
    assert table["Type"] == "AWS::DynamoDB::Table" and "TableName" not in table["Properties"]
    assert [key["AttributeName"] for key in table["Properties"]["KeySchema"]] == ["pool", "record"]
    # The two numbers of the configuration, as the script reads them, and the import path the
    # booking needs.
    assert script.check_of(given) == script.Check(
        stack=stack,
        logical_id=logical_id,
        wait_minutes=example.check_wait_minutes,
        session_max_hours=example.session_max_hours,
    )
    assert {"services/common", "scripts/pipeline"} <= set(given["PYTHONPATH"].split(":"))

    # The role may describe the resources of that one stack, and no other action of CloudFormation.
    (describe,) = verification.allowed("cloudformation")
    assert describe["Action"] == "cloudformation:DescribeStackResource"
    assert text(describe["Resource"]) == f"arn:<>:cloudformation:<>:<>:stack/{stack}/*"
    # It may read and write the items of that one table whose partition key is the name of a pool
    # the script books, the mock pool or the pool the decision names, and nothing else of DynamoDB
    # but its query of the heartbeats table.
    (items,) = [s for s in verification.allowed("dynamodb") if "dynamodb:Query" not in as_list(s["Action"])]
    assert sorted(as_list(items["Action"])) == ["dynamodb:GetItem", "dynamodb:PutItem"]
    assert text(items["Resource"]) == f"arn:<>:dynamodb:<>:<>:table/{stack}-{logical_id}-*"
    booked = [script.MOCK_POOL, decision.COURSEWORK_POOL]
    assert items["Condition"] == {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": booked}}
    assert set(booked) <= {pool.name for pool in example.pools}


def test_the_step_s_timeout_covers_the_wait_for_the_session_s_instance(
    verification: Verification, app_module: ModuleType, tmp_path: Path
) -> None:
    # The build outlasts the wait for each of the two pools the script books, the tests and the
    # stop of each session: a longer wait is a longer timeout, by twice as much, and beside the
    # waits there are hours for the rest.
    example = config.load({"RHI_CONFIG": "example"})
    beside_the_waits = verification.project["TimeoutInMinutes"] - 2 * example.check_wait_minutes
    assert beside_the_waits >= 120
    longer = build(app_module, tmp_path / "longer", lambda data: data.update(check_wait_minutes=180))
    assert longer.project["TimeoutInMinutes"] == 2 * 180 + beside_the_waits
    assert longer.given_by_the_project()[script.CHECK_WAIT_MINUTES_VARIABLE] == "180"
    # A wait CodeBuild's limit for one build cannot hold is refused when the app is built.
    with pytest.raises(ValueError, match="check_wait_minutes"):
        build(app_module, tmp_path / "too long", lambda data: data.update(check_wait_minutes=36 * 60))


def build(app_module: ModuleType, outdir: Path, change: Callable[[dict[str, Any]], None]) -> Verification:
    """The verification of the app's pipeline under the example configuration's data once `change`
    has changed it: the app as `app.py` builds it, cdk-nag included, with nothing bundled."""
    data = tomllib.loads((REPO / config.EXAMPLE_FILE).read_text(encoding="utf-8"))
    change(data)
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    app_module.build(config.parse(data, "test"), outdir=str(outdir), context=context).synth()
    return Verification(
        {
            path.relative_to(outdir).as_posix(): json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(outdir.rglob("*.template.json"))
        }
    )


def test_the_endpoint_is_the_gateway_stack_s_output_whatever_the_listener(
    verification: Verification, app_module: ModuleType, tmp_path: Path
) -> None:
    # The app's pipeline, whose configuration has a listener, and the same app with none.
    listening = verification
    silent = build(app_module, tmp_path, lambda data: data.update(https_listener=False))

    stack, value, _ = listening.output(script.ENDPOINT_VARIABLE)
    assert (stack, value) == (f"{STAGE}-Gateway", config.load({"RHI_CONFIG": "example"}).domain)
    stack, value, _ = silent.output(script.ENDPOINT_VARIABLE)
    assert (stack, value) == (f"{STAGE}-Gateway", script.NO_ENDPOINT)

    # The step's inputs do not change shape with the listener, and the pipeline's template holds
    # no value of either stage: only the names of the outputs.
    assert listening.given_by_the_action() == silent.given_by_the_action()
    assert script.ENDPOINT_VARIABLE not in silent.given_by_the_project()
    assert silent.action["RunOrder"] > max(action["RunOrder"] for action in silent.deployments())


def test_a_stage_with_no_pool_gives_no_parameter_to_read(app_module: ModuleType, tmp_path: Path) -> None:
    def leave_out_the_pools(data: dict[str, Any]) -> None:
        data["pools"] = {}
        del data["default_pool"]

    verification = build(app_module, tmp_path, leave_out_the_pools)
    _, value, _ = verification.output(script.RELEASE_PARAMETERS_VARIABLE)
    assert script.release_parameters(value) == {}
    assert verification.allowed("ssm") == []


def test_parked_the_verification_runs_no_test_and_leaves_parked_as_not_run(
    app_module: ModuleType, tmp_path: Path
) -> None:
    parked = build(app_module, tmp_path / "cdk.out", lambda data: data.update(parked=True))
    # Still the one action named Verify, after every deployment action of the stage, on the source.
    assert parked.action["ActionTypeId"]["Provider"] == "CodeBuild"
    assert parked.action["RunOrder"] > max(action["RunOrder"] for action in parked.deployments())
    (source,) = parked.pipeline["Stages"][0]["Actions"]
    assert parked.action["InputArtifacts"] == source["OutputArtifacts"]
    # No smoke test, no decision, no environment to run them in, and nothing read of the stage.
    commands = [command for phase in parked.phases.values() for command in phase["commands"]]
    assert not any(word in command for command in commands for word in ("uv ", "verify.py", "gpu_verification.py"))
    by_action, by_project = parked.given_by_the_action(), parked.given_by_the_project()
    assert list(by_action.values()) == [EXECUTION_ID]
    assert "OutputArtifacts" not in parked.action or not parked.action["OutputArtifacts"]

    # The post-build phase writes the one line parked to the execution's not-run mark, and passes;
    # a copy that fails fails it.
    tools, buckets = fake_aws(tmp_path), tmp_path / "buckets"
    (execution,) = [name for name, value in by_action.items() if value == EXECUTION_ID]
    (bucket,) = [name for name, value in by_project.items() if value == parked.pipeline["ArtifactStore"]["Location"]]

    def post_build(*, refuses: bool) -> tuple[int, dict[str, str]]:
        shutil.rmtree(buckets, ignore_errors=True)
        environ = {
            "PATH": f"{tools}:/usr/bin:/bin",
            "BUCKETS": str(buckets),
            "REFUSES": "yes" if refuses else "",
            script.REASON_FILE_VARIABLE: str(tmp_path / "reason"),
            bucket: "artifacts",
            execution: "an-execution",
        }
        script_text = "set -e\n" + "\n".join(parked.phases["post_build"]["commands"])
        exit_code = subprocess.run(["/bin/sh", "-c", script_text], env=environ, check=False).returncode
        objects = {
            path.relative_to(buckets).as_posix(): path.read_text(encoding="utf-8")
            for path in buckets.rglob("*")
            if path.is_file()
        }
        return exit_code, objects

    assert post_build(refuses=False) == (0, {"artifacts/verification/an-execution/not-run": "parked\n"})
    assert post_build(refuses=True)[0] != 0

    # The role writes under verification/ and holds nothing else but CodeBuild's own.
    services = {action.partition(":")[0] for s in parked.statements for action in as_list(s["Action"])}
    assert services == {"s3", "logs", "codebuild"}
    store = parked.pipeline["ArtifactStore"]["Location"]["Ref"]
    writes = [
        statement
        for statement in parked.allowed("s3")
        if not all(re.fullmatch(r"s3:(Get|List)\w*\*?", action) for action in as_list(statement["Action"]))
    ]
    assert [(w["Action"], text(w["Resource"])) for w in writes] == [("s3:PutObject", f"<{store}.Arn>/verification/*")]
