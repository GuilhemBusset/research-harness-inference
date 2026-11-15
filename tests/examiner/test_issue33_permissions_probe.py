"""Evaluate the real synthesized bootstrap policies without using their policy-test helpers."""

from fnmatch import fnmatchcase
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def policies(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("JSII_RUNTIME_PACKAGE_CACHE", "disabled")
        from aws_cdk import App, Stack
        from aws_cdk.assertions import Template
        from aws_cdk.cloudformation_include import CfnInclude

        app = App(outdir=str(tmp_path_factory.mktemp("bootstrap")), analytics_reporting=False)
        stack = Stack(app, "BoundedBootstrap")
        root = Path(__file__).resolve().parents[2]
        CfnInclude(stack, "Policies", template_file=str(root / "setup/bootstrap/policies.yaml"))
        resources = Template.from_stack(stack).to_json()["Resources"]
        return {
            resource["Properties"]["ManagedPolicyName"]: resource["Properties"]["PolicyDocument"]
            for resource in resources.values()
            if resource["Type"] == "AWS::IAM::ManagedPolicy"
        }


def values(value):
    return value if isinstance(value, list) else [value]


def resolve(value):
    if isinstance(value, dict):
        assert set(value) == {"Fn::Sub"}, value
        return value["Fn::Sub"].replace("${AWS::Partition}", "aws").replace("${AWS::AccountId}", "123456789012")
    return value


def denied(document, action, resource, context):
    for statement in values(document["Statement"]):
        if statement["Effect"] != "Deny":
            continue
        if not any(fnmatchcase(action.lower(), pattern.lower()) for pattern in values(statement.get("Action", []))):
            continue
        if not any(fnmatchcase(resource, resolve(pattern)) for pattern in values(statement.get("Resource", []))):
            continue
        matches = True
        for operator, keys in statement.get("Condition", {}).items():
            assert operator in {"StringNotEquals", "ArnNotEquals", "StringEquals", "ArnEquals", "Null"}, operator
            for key, expected in keys.items():
                present = context.get(key) in [resolve(value) for value in values(expected)]
                if operator in {"StringNotEquals", "ArnNotEquals"}:
                    matches &= not present
                elif operator == "Null":
                    matches &= (key not in context) == (str(expected).lower() == "true")
                else:
                    matches &= present
        if matches:
            return True
    return False


@pytest.mark.parametrize("name", ["rhi-cfn-execution", "rhi-permissions-boundary"])
def test_forbidden_requests_are_explicitly_denied(policies, name):
    document = policies[name]
    role = "arn:aws:iam::123456789012:role/example-role"
    for action, resource in [
        ("iam:CreateUser", "arn:aws:iam::123456789012:user/example-user"),
        ("iam:CreateAccessKey", "arn:aws:iam::123456789012:user/example-user"),
        ("iam:CreateLoginProfile", "arn:aws:iam::123456789012:user/example-user"),
        ("iam:CreateSAMLProvider", "arn:aws:iam::123456789012:saml-provider/example"),
        ("iam:CreateOpenIDConnectProvider", "arn:aws:iam::123456789012:oidc-provider/example.com"),
        ("iam:CreateServiceLinkedRole", "arn:aws:iam::123456789012:role/aws-service-role/example.com/example"),
        ("iam:DeleteRolePermissionsBoundary", role),
    ]:
        assert denied(document, action, resource, {}), (name, action)
    boundary = "arn:aws:iam::123456789012:policy/rhi-permissions-boundary"
    for action in ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]:
        for context in [{}, {"iam:PermissionsBoundary": "arn:aws:iam::123456789012:policy/another"}]:
            assert denied(document, action, role, context), (name, action, context)
        assert not denied(document, action, role, {"iam:PermissionsBoundary": boundary})
    for target in policies:
        for action in ["iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:DeletePolicy"]:
            assert denied(document, action, "arn:aws:iam::123456789012:policy/" + target, {}), (name, action, target)
