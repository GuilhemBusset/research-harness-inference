"""Independent issue 33 checks of the actual bootstrap template and policy tests."""

import copy
import importlib.util
import json
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
from cfnlint.api import lint
from cfnlint.decode import decode_str

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "setup/bootstrap/policies.yaml"
NAMES = {"rhi-cfn-execution", "rhi-permissions-boundary"}
PREFIX = "arn:aws:iam::123456789012:"
BOUNDARY = PREFIX + "policy/rhi-permissions-boundary"
REQUIRED = (
    "iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile",
    "iam:CreateSAMLProvider", "iam:CreateOpenIDConnectProvider",
    "iam:CreateRole", "iam:PutRolePermissionsBoundary",
    "iam:CreateServiceLinkedRole", "iam:DeleteRolePermissionsBoundary",
    "iam:CreatePolicyVersion", "iam:DeletePolicyVersion",
    "iam:SetDefaultPolicyVersion", "iam:DeletePolicy",
)


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def resources(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
        from aws_cdk import App, Stack
        from aws_cdk.assertions import Template
        from aws_cdk.cloudformation_include import CfnInclude

        app = App(outdir=str(tmp_path_factory.mktemp("bootstrap")), analytics_reporting=False)
        stack = Stack(app, "BootstrapProbe")
        CfnInclude(stack, "Policies", template_file=str(TARGET))
        return Template.from_stack(stack).to_json()["Resources"]


def values(value):
    return value if isinstance(value, list) else [value]


def resolve(value):
    if isinstance(value, dict):
        assert set(value) == {"Fn::Sub"}
        value = value["Fn::Sub"]
    return value.replace("${AWS::Partition}", "aws").replace("${AWS::AccountId}", "123456789012")


def applies(statement, action, resource, context):
    if not any(fnmatchcase(action.lower(), pattern.lower()) for pattern in values(statement.get("Action", []))):
        return False
    if not any(fnmatchcase(resource, resolve(pattern)) for pattern in values(statement.get("Resource", []))):
        return False
    for operator, entries in statement.get("Condition", {}).items():
        assert operator in {"StringEquals", "StringNotEquals", "ArnEquals", "ArnNotEquals"}
        for key, expected in entries.items():
            equal = context.get(key) in [resolve(value) for value in values(expected)]
            if equal != (operator in {"StringEquals", "ArnEquals"}):
                return False
    return True


def effects(document, action, resource, context=None):
    return {
        statement["Effect"] for statement in values(document["Statement"])
        if applies(statement, action, resource, context or {})
    }


def test_fixed_managed_policy_resources(resources):
    assert len(resources) == 2
    assert {resource["Type"] for resource in resources.values()} == {"AWS::IAM::ManagedPolicy"}
    assert {resource["Properties"]["ManagedPolicyName"] for resource in resources.values()} == NAMES
    assert all(resource["Properties"].get("Path", "/") == "/" for resource in resources.values())
    assert "${AWS::AccountId}" in TARGET.read_text()
    assert "${AWS::Partition}" in TARGET.read_text()


@pytest.mark.parametrize("name", sorted(NAMES))
def test_request_denials_in_synthesized_policies(resources, name):
    policy = next(resource["Properties"]["PolicyDocument"] for resource in resources.values()
                  if resource["Properties"]["ManagedPolicyName"] == name)
    role = PREFIX + "role/example"
    for action in REQUIRED:
        if action in {"iam:CreateRole", "iam:PutRolePermissionsBoundary"}:
            for context in [{}, {"iam:PermissionsBoundary": PREFIX + "policy/other"}]:
                assert "Deny" in effects(policy, action, role, context), (name, action, context)
            valid = effects(policy, action, role, {"iam:PermissionsBoundary": BOUNDARY})
            assert "Allow" in valid and "Deny" not in valid
        elif action in {"iam:CreatePolicyVersion", "iam:DeletePolicyVersion",
                        "iam:SetDefaultPolicyVersion", "iam:DeletePolicy"}:
            for target in NAMES:
                assert "Deny" in effects(policy, action, PREFIX + "policy/" + target)
        else:
            target = role
            if action in {"iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile"}:
                target = PREFIX + "user/example"
            elif action == "iam:CreateSAMLProvider":
                target = PREFIX + "saml-provider/example"
            elif action == "iam:CreateOpenIDConnectProvider":
                target = PREFIX + "oidc-provider/example.com"
            assert "Deny" in effects(policy, action, target), (name, action)


@pytest.mark.parametrize("logical_id", ["ExecutionPolicy", "PermissionsBoundary"])
@pytest.mark.parametrize("action", REQUIRED)
def test_policy_test_rejects_each_removed_denial(logical_id, action, tmp_path, monkeypatch):
    checker = load("tests/policy/test_bootstrap_policies.py", "bootstrap_policy_probe")
    checker.test_the_two_policies_hold_every_denial()
    template, errors = decode_str(TARGET.read_text())
    assert not errors
    assert template is not None
    changed = copy.deepcopy(template)
    for statement in changed["Resources"][logical_id]["Properties"]["PolicyDocument"]["Statement"]:
        if statement["Effect"] == "Deny":
            statement["Action"] = [entry for entry in values(statement["Action"]) if entry != action]
    target = tmp_path / "policies.json"
    target.write_text(json.dumps(changed))
    monkeypatch.setattr(checker, "TEMPLATE", target)
    with pytest.raises(AssertionError):
        checker.test_the_two_policies_hold_every_denial()


def test_bootstrap_template_is_linted():
    checker = load("tests/synth/test_cfn_lint.py", "bootstrap_lint_probe")
    assert TARGET in checker.setup_templates(ROOT / "setup")
    seen = []

    def recording_lint(text):
        seen.append(text)
        return lint(text)

    checker.findings = recording_lint
    checker.test_every_template_under_setup_passes()
    assert TARGET.read_text() in seen
