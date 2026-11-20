"""The spike jobs of `src/rhi/pipeline/spikes.py` (system design, sections 8.4 and 12).

A job is a project of the pipeline's stack for each entry of a list, and only for an entry. Every
job runs on one role, logs to one log group and is encrypted with one key, all three created once
whatever the list holds, so that a new entry adds a project and nothing else: no statement on the
role and no cdk-nag finding.

Each test builds the app as `app.py` does, from the example configuration, with a list of its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
CONTEXT = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
# Two entries, as a spike's issue writes one: the job's name and its script under `spikes/`.
TWO = (("first_spike", "first_spike.py"), ("second_spike", "second_spike.py"))
STANDARD_IMAGE = "aws/codebuild/standard:7.0"


@dataclass(frozen=True)
class Built:
    """The pipeline's template, the logical IDs of the shared role, log group and key and of each
    job's project by its name, and the findings that failed synthesis, each as its id and the path
    of its resource."""

    template: dict[str, Any]
    role: str
    log_group: str
    key: str
    projects: dict[str, str]
    findings: list[tuple[str, str]]


def build(app_module: ModuleType, folder: Path, entries: tuple[tuple[str, str], ...], *, acknowledged: bool) -> Built:
    """The app with the spike jobs of `entries`, synthesized under cdk-nag with the repository's
    acknowledgement files, or with none, so that every finding is reported."""
    import aws_cdk as cdk

    from rhi.pipeline.spikes import SpikeJob
    from rhi.pipeline.stack import PipelineStack
    from rhi.stage import EndpointStage

    example = config.load({"RHI_CONFIG": "example"})
    outdir = folder / "cdk.out"
    app = cdk.App(outdir=str(outdir), context=CONTEXT, analytics_reporting=False)
    stage = EndpointStage(app, "Rhi", config=example)
    if acknowledged:
        app_module.add_nag(app)
    else:
        (folder / "none").mkdir(parents=True)
        app_module.add_nag(app, acknowledged=folder / "none")
    jobs = [SpikeJob(name, script) for name, script in entries]
    stack = PipelineStack(app, "RhiPipeline", config=example, stage=stage, spike_jobs=jobs)
    if acknowledged:
        app.synth()
    else:
        with pytest.raises(RuntimeError):
            app.synth()
    report = json.loads((outdir / "validation-report.json").read_text(encoding="utf-8"))
    findings = sorted(
        (violation["ruleName"], construct["constructPath"])
        for plugin in report["pluginReports"]
        if plugin["conclusion"] == "failure"
        for violation in plugin["violations"]
        for construct in violation["violatingConstructs"]
    )

    def logical_id(construct: Any) -> str:
        return stack.resolve(stack.get_logical_id(construct.node.default_child))

    spikes = stack.spikes
    return Built(
        template=json.loads((outdir / stack.template_file).read_text(encoding="utf-8")),
        role=logical_id(spikes.role),
        log_group=logical_id(spikes.log_group),
        key=logical_id(spikes.key),
        projects={name: logical_id(project) for name, project in spikes.projects.items()},
        findings=findings,
    )


@pytest.fixture(scope="module")
def two(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Built:
    """The app with two jobs, which synthesizes under the repository's acknowledgement files."""
    return build(app_module, tmp_path_factory.mktemp("two"), TWO, acknowledged=True)


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: r.get("Properties", {}) for name, r in template["Resources"].items() if r["Type"] == kind}


def runs_a_spike(project: dict[str, Any]) -> list[str]:
    """The commands of a project's build specification that run a script under `spikes/`. A
    specification built from values of the template, as CDK Pipelines writes some, runs none."""
    spec = project["Source"].get("BuildSpec")
    if not isinstance(spec, str):
        return []
    phases = json.loads(spec)["phases"]
    return [command for phase in phases.values() for command in phase.get("commands", []) if " spikes/" in command]


def text(value: Any) -> str:
    """A value of the template as text: a reference as `<Name>` or `<Name.Attribute>`."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join(text(part) for part in value)
    ((function, argument),) = value.items()
    if function == "Ref":
        return f"<{argument}>"
    if function == "Fn::GetAtt":
        return "<" + ".".join(argument) + ">"
    assert function == "Fn::Join", value
    return argument[0].join(text(part) for part in argument[1])


def statements(built: Built) -> list[tuple[tuple[str, ...], str, str]]:
    """Every statement the shared role is allowed, as `(actions, resources, condition)`, sorted."""
    held = [
        statement
        for policy in of_type(built.template, "AWS::IAM::Policy").values()
        if {"Ref": built.role} in policy["Roles"]
        for statement in policy["PolicyDocument"]["Statement"]
    ]
    assert built.role in built.template["Resources"]
    assert "ManagedPolicyArns" not in built.template["Resources"][built.role]["Properties"]
    assert "Policies" not in built.template["Resources"][built.role]["Properties"]
    assert all(statement["Effect"] == "Allow" for statement in held)
    return sorted(
        (
            tuple(statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]),
            text(statement["Resource"]),
            json.dumps(statement.get("Condition", {}), sort_keys=True),
        )
        for statement in held
    )


def test_a_list_of_two_gives_two_jobs_each_running_its_own_script(two: Built) -> None:
    projects = of_type(two.template, "AWS::CodeBuild::Project")
    spike_jobs = {name: project for name, project in projects.items() if runs_a_spike(project)}
    assert sorted(spike_jobs) == sorted(two.projects.values())
    for name, script in TWO:
        project = projects[two.projects[name]]
        assert runs_a_spike(project) == [f"uv run --locked python spikes/{script}"]
        assert project["Name"] == f"rhi-spike-{name}"
        # Started by the owner on a commit of `main`, its head by default, read through the
        # pipeline's connection, with nothing that starts it.
        assert project["SourceVersion"] == "main"
        assert project["Source"]["Auth"] == {
            "Type": "CODECONNECTIONS",
            "Resource": {"Fn::GetAtt": ["Connection", "ConnectionArn"]},
        }
        assert (project["Source"]["Type"], project["Source"]["ReportBuildStatus"]) == ("GITHUB", False)
        assert project.get("Triggers", {"Webhook": False}) == {"Webhook": False}
        # The locked environment, with the package roots on the import path.
        phases = json.loads(project["Source"]["BuildSpec"])["phases"]
        assert phases["build"]["commands"][0] == "uv sync --locked"
        variables = {variable["Name"]: variable["Value"] for variable in project["Environment"]["EnvironmentVariables"]}
        assert {"src", "services/common", "spikes"} <= set(variables["PYTHONPATH"].split(":"))
        assert variables["RHI_MEASUREMENT_BUS"] == "rhi-measurements"
        # What the AWS Solutions pack asks of a project, fixed: the standard image, not privileged.
        assert (project["Environment"]["Image"], project["Environment"]["PrivilegedMode"]) == (STANDARD_IMAGE, False)


def test_an_empty_list_gives_none(app_module: ModuleType, tmp_path: Path, templates: dict[str, Any]) -> None:
    from rhi.pipeline.spikes import JOBS

    empty = build(app_module, tmp_path, (), acknowledged=True)
    projects = of_type(empty.template, "AWS::CodeBuild::Project").values()
    assert empty.projects == {} and not [project for project in projects if runs_a_spike(project)]
    # The role, the log group and the key are there all the same.
    assert {empty.role, empty.log_group, empty.key} <= set(empty.template["Resources"])
    # The app's own list gives one job for each of its entries, and no other.
    (pipeline,) = [template for path, template in templates.items() if path == "RhiPipeline.template.json"]
    found = sorted(
        command
        for project in of_type(pipeline, "AWS::CodeBuild::Project").values()
        for command in runs_a_spike(project)
    )
    assert found == sorted(f"uv run --locked python spikes/{job.script}" for job in JOBS)


def test_every_job_runs_on_the_one_role_log_group_and_key(app_module: ModuleType, tmp_path: Path, two: Built) -> None:
    for name in dict(TWO):
        project = of_type(two.template, "AWS::CodeBuild::Project")[two.projects[name]]
        assert project["ServiceRole"] == {"Fn::GetAtt": [two.role, "Arn"]}
        assert project["LogsConfig"] == {"CloudWatchLogs": {"GroupName": {"Ref": two.log_group}, "Status": "ENABLED"}}
        assert project["EncryptionKey"] == {"Fn::GetAtt": [two.key, "Arn"]}
    # One of each in the template, and the shared log group under CodeBuild's prefix.
    assert [len(of_type(two.template, kind)) for kind in ("AWS::KMS::Key", "AWS::CodeBuild::ReportGroup")] == [1, 0]
    assert of_type(two.template, "AWS::Logs::LogGroup")[two.log_group]["LogGroupName"] == "/aws/codebuild/rhi-spikes"
    assert of_type(two.template, "AWS::KMS::Key")[two.key]["EnableKeyRotation"] is True
    roles = {
        project["ServiceRole"]["Fn::GetAtt"][0]
        for project in of_type(two.template, "AWS::CodeBuild::Project").values()
        if runs_a_spike(project)
    }
    assert roles == {two.role}

    # What the role may do, which is what the Context lists and nothing else.
    held = statements(two)
    table = "Rhi-Pools-TablesPoolState68A872F7-*"
    here = "arn:<AWS::Partition>:{}:<AWS::Region>:<AWS::AccountId>:{}"
    assert held == sorted(
        [
            (
                ("ssm:GetParametersByPath",),
                ",".join(here.format("ssm", f"parameter/rhi/config{s}") for s in ("", "/*")),
                "{}",
            ),
            (("dynamodb:GetItem", "dynamodb:Query"), here.format("dynamodb", f"table/{table}"), "{}"),
            (("secretsmanager:GetSecretValue",), here.format("secretsmanager", "secret:rhi/spike-keys-??????"), "{}"),
            (
                ("aps:QueryMetrics",),
                here.format("aps", "workspace/*"),
                json.dumps({"StringEquals": {"aws:ResourceTag/rhi-stage": "Rhi"}}),
            ),
            (("events:PutEvents",), here.format("events", "event-bus/rhi-measurements"), "{}"),
            (
                ("codeconnections:GetConnection", "codeconnections:GetConnectionToken"),
                "<Connection.ConnectionArn>",
                "{}",
            ),
            (("logs:CreateLogStream", "logs:PutLogEvents"), f"<{two.log_group}.Arn>", "{}"),
            (("kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:GenerateDataKey"), f"<{two.key}.Arn>", "{}"),
        ]
    )
    # The table is the stage's pool-state table, by the logical ID recorded for it.
    recorded = json.loads((REPO / "tests/policy/stateful_logical_ids.json").read_text(encoding="utf-8"))
    assert table in {f"{stack}-{logical_id}-*" for stack, ids in recorded.items() for logical_id in ids}

    # The same statements whatever the list holds: none, one or two entries.
    for entries in ((), TWO[:1]):
        other = build(app_module, tmp_path / str(len(entries)), entries, acknowledged=True)
        assert statements(other) == held


def test_a_new_entry_brings_no_new_finding(app_module: ModuleType, tmp_path: Path, two: Built) -> None:
    # With no acknowledgement at all, so that every finding is reported.
    one = build(app_module, tmp_path / "one", TWO[:1], acknowledged=False)
    both = build(app_module, tmp_path / "two", TWO, acknowledged=False)
    assert one.findings and {rule for rule, _ in both.findings} == {rule for rule, _ in one.findings}
    # None of them is on a job's project.
    assert not [path for _, path in both.findings if path.startswith("RhiPipeline/Spikes/") and "/Role/" not in path]
    # Both jobs name the one role and the one log group.
    projects = [of_type(two.template, "AWS::CodeBuild::Project")[logical_id] for logical_id in two.projects.values()]
    assert len(projects) == 2
    assert len({json.dumps(project["ServiceRole"]) for project in projects}) == 1
    assert len({json.dumps(project["LogsConfig"]) for project in projects}) == 1
