"""Independent checks for issue 30's linting gate."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from cfnlint.api import lint

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("issue30_lint", REPO / "tests/synth/test_cfn_lint.py")
assert SPEC is not None and SPEC.loader is not None
SUBJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBJECT)


def test_real_assembly_templates_are_all_linted(templates, monkeypatch):
    seen = []

    def recording_lint(text):
        seen.append(json.loads(text))
        return lint(text)

    monkeypatch.setattr(SUBJECT, "lint", recording_lint)
    SUBJECT.test_every_synthesized_template_passes(templates)
    assert templates
    assert seen == list(templates.values())
    for path in templates:
        damaged = copy.deepcopy(templates)
        damaged[path].setdefault("Resources", {})["InvalidResource"] = {"Type": "AWS::SQS::UnknownResource"}
        with pytest.raises(AssertionError):
            SUBJECT.test_every_synthesized_template_passes(damaged)


def test_nested_setup_templates_are_linted_and_errors_fail(tmp_path, monkeypatch):
    setup = tmp_path / "setup"
    nested = setup / "bootstrap" / "nested"
    nested.mkdir(parents=True)
    yaml_path = nested / "policy.yaml"
    yaml_path.write_text(
        "Resources:\n  Topic:\n    Type: AWS::SNS::Topic\n    Properties:\n"
        "      DisplayName: !Sub '${AWS::StackName} alerts'\n"
    )
    json_path = setup / "queue.json"
    json_path.write_text(json.dumps({"Resources": {"Queue": {"Type": "AWS::SQS::Queue"}}}))
    (setup / "ruleset.json").write_text(json.dumps({"name": "pr-only", "rules": []}))
    monkeypatch.setattr(SUBJECT, "REPO", tmp_path)
    monkeypatch.setattr(SUBJECT, "SETUP", setup)
    seen = []

    def recording_lint(text):
        seen.append(text)
        return lint(text)

    monkeypatch.setattr(SUBJECT, "lint", recording_lint)
    SUBJECT.test_every_template_under_setup_passes()
    assert set(seen) == {yaml_path.read_text(), json_path.read_text()}
    for path in (yaml_path, json_path):
        original = path.read_text()
        path.write_text(json.dumps({"Resources": {"InvalidResource": {"Type": "AWS::SQS::UnknownResource"}}}))
        with pytest.raises(AssertionError):
            SUBJECT.test_every_template_under_setup_passes()
        path.write_text(original)
