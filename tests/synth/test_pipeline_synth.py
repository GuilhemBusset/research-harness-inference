"""The pipeline's synth step (src/rhi/pipeline/synth.py; system design, section 8.2, step 2): its
commands in order, the fetched configuration outside the source tree, the import path of the
tests, and a role that reads the configuration's parameters and nothing else of SSM."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import PurePosixPath
from typing import Any

from rhi import config

PIPELINE_TEMPLATE = "RhiPipeline.template.json"

# The step's commands, in the order they must run: each pattern matches one command.
IN_ORDER = [
    ("a pinned uv", r"python3 -m pip install uv==\d+\.\d+\.\d+"),
    ("the CDK CLI of the npm lock file", r"npm ci( .*)?"),
    ("the locked environment", r"uv sync --locked"),
    ("the configuration fetch", r"uv run --locked python scripts/pipeline/fetch_config\.py"),
    ("the leak check", r"uv run --locked python scripts/pipeline/leak_check\.py"),
    ("synthesis from the fetched file", r"RHI_CONFIG=file node_modules/\.bin/cdk synth --lookups=false"),
    ("a git work tree for the tests", r"git init( .*)?"),
    ("the tests", r"uv run --locked pytest( .*)?"),
]
# Where CodeBuild checks the source out.
CODEBUILD_SOURCES = PurePosixPath("/codebuild")


def synth_project(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The properties of the project the pipeline's synth action runs."""
    resources = templates[PIPELINE_TEMPLATE]["Resources"]
    (pipeline,) = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::CodePipeline::Pipeline"]
    (build,) = [stage for stage in pipeline["Stages"] if stage["Name"] == "Build"]
    (synth,) = build["Actions"]
    return resources[synth["Configuration"]["ProjectName"]["Ref"]]["Properties"]


def environment_of(project: dict[str, Any]) -> dict[str, str]:
    return {variable["Name"]: variable["Value"] for variable in project["Environment"]["EnvironmentVariables"]}


def test_the_commands_run_in_order(templates: dict[str, dict[str, Any]]) -> None:
    phases = json.loads(synth_project(templates)["Source"]["BuildSpec"])["phases"]
    commands = [command for phase in ("install", "build") for command in phases[phase]["commands"]]
    at = {
        what: [index for index, command in enumerate(commands) if re.fullmatch(pattern, command)]
        for what, pattern in IN_ORDER
    }
    assert all(len(found) == 1 for found in at.values()), at
    order = [found[0] for found in at.values()]
    assert order == sorted(order), commands
    # Every command but the install runs in the build phase, after the install.
    assert phases["install"]["commands"] == commands[: order[2]]


def test_the_fetched_file_is_outside_the_source_tree(templates: dict[str, dict[str, Any]]) -> None:
    project = synth_project(templates)
    named = PurePosixPath(environment_of(project)["RHI_CONFIG_FILE"])
    assert named.is_absolute()
    assert ".." not in named.parts
    assert not named.is_relative_to(CODEBUILD_SOURCES)
    # No command names another file, or another source, than the step's.
    commands = json.loads(project["Source"]["BuildSpec"])["phases"]["build"]["commands"]
    assert not any("RHI_CONFIG_FILE" in command for command in commands)


def test_pythonpath_holds_the_folders_of_pytests_import_path(templates: dict[str, dict[str, Any]]) -> None:
    pyproject = tomllib.loads((config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    folders = pyproject["tool"]["pytest"]["ini_options"]["pythonpath"]
    assert environment_of(synth_project(templates))["PYTHONPATH"].split(":") == folders


def test_the_synth_role_reads_the_configuration_by_its_path(templates: dict[str, dict[str, Any]]) -> None:
    resources = templates[PIPELINE_TEMPLATE]["Resources"]
    role = synth_project(templates)["ServiceRole"]["Fn::GetAtt"][0]
    statements = [
        statement
        for policy in resources.values()
        if policy["Type"] == "AWS::IAM::Policy" and {"Ref": role} in policy["Properties"]["Roles"]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    (read,) = [statement for statement in statements if "ssm:" in json.dumps(statement["Action"])]
    assert read["Action"] == "ssm:GetParametersByPath"
    arns = ["".join(part if isinstance(part, str) else "<>" for part in r["Fn::Join"][1]) for r in read["Resource"]]
    assert arns == ["arn:<>:ssm:<>:<>:parameter/rhi/config", "arn:<>:ssm:<>:<>:parameter/rhi/config/*"]
    assert not any("secretsmanager:" in json.dumps(statement["Action"]) for statement in statements)
