"""cfn-lint over every CloudFormation template of the repository (system design, section 8.3).

That is every template the app synthesizes from the example configuration, those of nested stage
assemblies included, and every template kept under `setup/`. cfn-lint runs at its defaults: its
errors and its warnings, against the resource specification of its default region, since no stack
names one. A template passes when cfn-lint finds nothing. It reads the specification it ships with,
so no test needs the network.
"""

from __future__ import annotations

import importlib.metadata
import json
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cfnlint.api import lint
from cfnlint.decode import decode_str

if TYPE_CHECKING:
    from cfnlint.match import Match

REPO = Path(__file__).resolve().parents[2]
SETUP = REPO / "setup"
# What a CloudFormation template is saved as.
TEMPLATE_SUFFIXES = {".json", ".template", ".yaml", ".yml"}


def findings(text: str) -> list[Match]:
    """What cfn-lint finds in a template, given as YAML or JSON text."""
    return lint(text)


def place(finding: Match) -> list[Any]:
    """Where in the template a finding points: the keys from the root down."""
    return list(getattr(finding, "path", []))


def synthesized_findings(template: dict[str, Any]) -> list[Match]:
    """What cfn-lint finds in a template the app synthesized.

    One finding is left out, for a stack that holds no resource yet: that the template has no
    `Resources` section. CloudFormation refuses such a template, and the stage's stack is one until
    its first construct arrives (`src/rhi/stage.py`). Everything else of that template is still
    linted, and a template with a `Resources` section gets no exception.
    """
    found = findings(json.dumps(template))
    if "Resources" in template:
        return found
    return [
        finding
        for finding in found
        # The template's own schema (rule E1001), at the root, on a section that is required:
        # `Resources` is the only one.
        if (finding.rule.id, place(finding), getattr(finding, "validator", None)) != ("E1001", [], "required")
    ]


def setup_templates(folder: Path) -> list[Path]:
    """Every CloudFormation template under a folder, at any depth.

    A YAML or JSON file is one when it has a `Resources` section or names the template format, so
    the GitHub ruleset beside the templates is not. A file that cannot be read as YAML or JSON
    counts as one: cfn-lint then reports why, where leaving it out would hide a broken template.
    """
    found = []
    for path in sorted(folder.rglob("*")):
        if path.suffix not in TEMPLATE_SUFFIXES or not path.is_file():
            continue
        # cfn-lint's reader, which knows the short forms of the intrinsic functions (`!Sub`).
        content, unreadable = decode_str(path.read_text(encoding="utf-8"))
        if unreadable or (isinstance(content, dict) and content.keys() & {"Resources", "AWSTemplateFormatVersion"}):
            found.append(path)
    return found


def test_cfn_lint_is_pinned_in_the_dev_group() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    pins = [
        requirement
        for requirement in pyproject["dependency-groups"]["dev"]
        if isinstance(requirement, str) and requirement.startswith("cfn-lint")
    ]
    locked = [package["version"] for package in lock["package"] if package["name"] == "cfn-lint"]
    assert len(locked) == 1
    assert pins == [f"cfn-lint=={locked[0]}"]
    assert importlib.metadata.version("cfn-lint") == locked[0]


def test_every_synthesized_template_passes(templates: dict[str, dict[str, Any]]) -> None:
    assert templates
    found = {
        path: [repr(finding) for finding in synthesized_findings(template)] for path, template in templates.items()
    }
    assert {path: of_path for path, of_path in found.items() if of_path} == {}


def test_every_template_under_setup_passes() -> None:
    found = {
        path.relative_to(REPO).as_posix(): [repr(finding) for finding in findings(path.read_text(encoding="utf-8"))]
        for path in setup_templates(SETUP)
    }
    assert {path: of_path for path, of_path in found.items() if of_path} == {}


def test_the_bootstrap_policies_are_among_the_templates_under_setup() -> None:
    # The template the owner deploys by hand before the bootstrap (setup/bootstrap/README.md).
    assert SETUP / "bootstrap" / "policies.yaml" in setup_templates(SETUP)


def test_reports_an_error() -> None:
    template = {"Resources": {"Queue": {"Type": "AWS::SQS::Queue"}}}
    assert synthesized_findings(template) == []

    template["Resources"]["Queue"]["Type"] = "AWS::SQS::NoSuchType"
    found = synthesized_findings(template)
    assert [(finding.rule.severity, place(finding)[:2]) for finding in found] == [("error", ["Resources", "Queue"])]


def test_a_stack_with_no_resource_is_excused_nothing_else() -> None:
    no_resource: dict[str, Any] = {
        "Parameters": {"Name": {"Type": "String"}},
        "Outputs": {"Name": {"Value": {"Ref": "Name"}}},
    }
    assert synthesized_findings(no_resource) == []

    # Another finding of the same rule, in the same template.
    found = synthesized_findings(no_resource | {"NoSuchSection": {}})
    assert [(finding.rule.severity, place(finding)) for finding in found] == [("error", ["NoSuchSection"])]
    # An empty `Resources` section is not what the CDK writes for a stack with no resource.
    assert synthesized_findings({"Resources": {}}) != []


def test_the_templates_under_a_folder_are_found(tmp_path: Path) -> None:
    short_form = (
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Resources:\n"
        "  Topic:\n"
        "    Type: AWS::SNS::Topic\n"
        "    Properties:\n"
        "      DisplayName: !Sub '${AWS::StackName} alarms'\n"
    )
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "bootstrap" / "policies.yaml").write_text(short_form, encoding="utf-8")
    (tmp_path / "stack.json").write_text(json.dumps({"Resources": {"Queue": {"Type": "AWS::SQS::Queue"}}}), "utf-8")
    (tmp_path / "broken.yml").write_text("Resources: [unclosed\n", encoding="utf-8")
    # Not templates: the shape of the GitHub ruleset, and the prose beside it.
    (tmp_path / "ruleset.json").write_text(json.dumps({"name": "pr-only", "rules": []}), encoding="utf-8")
    (tmp_path / "README.md").write_text("Resources:\n", encoding="utf-8")

    found = [path.relative_to(tmp_path).as_posix() for path in setup_templates(tmp_path)]

    assert found == ["bootstrap/policies.yaml", "broken.yml", "stack.json"]
    assert findings(short_form) == []
    assert findings((tmp_path / "broken.yml").read_text(encoding="utf-8")) != []
