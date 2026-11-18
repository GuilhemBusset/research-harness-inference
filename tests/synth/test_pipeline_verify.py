"""The pipeline's verification step (src/rhi/pipeline/verify.py; system design, section 8.2, step
6): the action named Verify, after the stage's deployment, which runs the verification script and
builds no image; a post-build phase that copies the script's reason, when there is one, to the
key verification/<execution ID>/not-run of the artifact bucket; inputs that are outputs of the
stage's stacks and the execution's ID; and a role that reads the canary's secret, queries the
stage's workspace, reads the stage's pools' release parameters and writes under verification/.

The tests read the app's own pipeline, the one that is deployed. Two of them also build the app
from a changed configuration, with no listener and with no pool.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

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
    # The script is a command of its own, after the locked environment: its exit is the build's.
    assert build == ["uv sync --locked", "uv run --locked python scripts/pipeline/verify.py"]
    assert (REPO / "scripts/pipeline/verify.py").is_file()
    assert "VpcConfig" not in verification.project
    assert verification.project["Environment"]["PrivilegedMode"] is False
    commands = [command for phase in verification.phases.values() for command in phase["commands"]]
    assert not any("docker" in command for command in commands)


def test_the_post_build_phase_copies_the_reason_when_there_is_one(verification: Verification, tmp_path: Path) -> None:
    by_action, by_project = verification.given_by_the_action(), verification.given_by_the_project()
    (execution,) = [name for name, value in by_action.items() if value == EXECUTION_ID]
    (store,) = [verification.pipeline["ArtifactStore"]["Location"]]
    (bucket,) = [name for name, value in by_project.items() if value == store]
    reason_file = script.REASON_FILE_VARIABLE
    # The file the step names is outside the checkout, which CodeBuild places under its own folder.
    assert Path(by_project[reason_file]).is_absolute() and not by_project[reason_file].startswith("/codebuild")

    # The AWS CLI, as far as the phase uses it: `aws s3 cp <file> s3://<bucket>/<key>`, into a folder.
    tools, buckets = tmp_path / "tools", tmp_path / "buckets"
    tools.mkdir()
    (tools / "aws").write_text(
        '#!/bin/sh\n[ "$#" = 4 ] && [ "$1 $2" = "s3 cp" ] || exit 64\n[ -z "$REFUSES" ] || exit 1\n'
        'to="$BUCKETS/${4#s3://}"\nmkdir -p "$(dirname "$to")" && cp "$3" "$to"\n',
        encoding="utf-8",
    )
    (tools / "aws").chmod(0o755)

    def post_build(reason: str | None, *, refuses: bool = False) -> tuple[int, dict[str, str]]:
        """Runs the phase for the execution `an-execution`, after a script that left `reason`:
        its exit, and the objects of the buckets."""
        left = tmp_path / "left" / "reason"
        left.parent.mkdir(exist_ok=True)
        left.unlink(missing_ok=True)
        if reason is not None:
            left.write_text(reason, encoding="utf-8")
        environ = {
            "PATH": f"{tools}:/usr/bin:/bin",
            "BUCKETS": str(buckets),
            "REFUSES": "yes" if refuses else "",
            reason_file: str(left),
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

    # No reason: every test ran, and nothing is written.
    assert post_build(None) == (0, {})
    # A copy that fails fails the build: a verification that was not run is not taken for one that passed.
    assert post_build("canary-missing\n", refuses=True)[0] != 0
    assert post_build("listener-off\n") == (0, {"artifacts/verification/an-execution/not-run": "listener-off\n"})


def test_the_inputs_are_outputs_of_the_stage_s_stacks_and_the_execution_s_id(verification: Verification) -> None:
    by_action = verification.given_by_the_action()
    (execution,) = [name for name, value in by_action.items() if value == EXECUTION_ID]
    assert set(by_action) == {
        execution,
        script.ENDPOINT_VARIABLE,
        "RHI_SMOKE_WORKSPACE_URL",
        script.RELEASE_PARAMETERS_VARIABLE,
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

    # Beside the four grants, only CodeBuild's own: the project's logs and test reports.
    services = {action.partition(":")[0] for s in verification.statements for action in as_list(s["Action"])}
    assert services == {"secretsmanager", "s3", "aps", "ssm", "logs", "codebuild"}
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

    # Each parameter the step is given, by its name, and no pattern.
    (read,) = verification.allowed("ssm")
    assert read["Action"] == "ssm:GetParameter"
    _, value, _ = verification.output(script.RELEASE_PARAMETERS_VARIABLE)
    given = script.release_parameters(value)
    assert sorted(text(r) for r in as_list(read["Resource"])) == sorted(
        f"arn:<>:ssm:<>:<>:parameter{name}" for name in given.values()
    )


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
