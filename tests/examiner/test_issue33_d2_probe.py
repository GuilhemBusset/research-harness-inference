"""Independent checks of the hand-authored bootstrap template; no AWS calls."""

import json
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
from cfnlint.decode import decode_str

ROOT = Path(__file__).resolve().parents[2]
POLICIES = ROOT / "setup/bootstrap/policies.yaml"
PREFIX = "arn:aws:iam::123456789012:"
BOUNDARY = PREFIX + "policy/rhi-permissions-boundary"
EXECUTION = PREFIX + "policy/rhi-cfn-execution"
ROLE = PREFIX + "role/probe"
ACTIONS = [
    "iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile",
    "iam:CreateSAMLProvider", "iam:CreateOpenIDConnectProvider", "iam:CreateRole",
    "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary",
    "iam:CreatePolicyVersion", "iam:DeletePolicyVersion",
    "iam:SetDefaultPolicyVersion", "iam:DeletePolicy",
]


def read():
    template, errors = decode_str(POLICIES.read_text())
    assert not errors
    return json.loads(json.dumps(template))


def values(value):
    return value if isinstance(value, list) else [value]


def resolve(value):
    if isinstance(value, dict):
        value = value["Fn::Sub"]
    return value.replace("${AWS::Partition}", "aws").replace("${AWS::AccountId}", "123456789012")


def applicable(statement, action, resource, context):
    if "NotAction" in statement:
        action_matches = not any(fnmatchcase(action.lower(), pattern.lower()) for pattern in values(statement["NotAction"]))
    else:
        action_matches = any(fnmatchcase(action.lower(), pattern.lower()) for pattern in values(statement.get("Action", [])))
    if "NotResource" in statement:
        resource_matches = not any(fnmatchcase(resource, resolve(pattern)) for pattern in values(statement["NotResource"]))
    else:
        resource_matches = any(fnmatchcase(resource, resolve(pattern)) for pattern in values(statement.get("Resource", [])))
    if not action_matches or not resource_matches:
        return False
    for operator, entries in statement.get("Condition", {}).items():
        for key, expected in entries.items():
            actual = context.get(key)
            equal = actual in [resolve(value) for value in values(expected)]
            if operator in {"StringEquals", "ArnEquals"}:
                matches = equal
            elif operator in {"StringNotEquals", "ArnNotEquals"}:
                matches = not equal
            else:
                raise AssertionError(f"Unsupported condition in probe: {operator}")
            if not matches:
                return False
    return True


def decision(document, action, resource, context=None):
    effects = {
        statement["Effect"] for statement in values(document["Statement"])
        if applicable(statement, action, resource, context or {})
    }
    return "Deny" if "Deny" in effects else "Allow" if "Allow" in effects else "ImplicitDeny"


@pytest.mark.parametrize("logical_id", ["ExecutionPolicy", "PermissionsBoundary"])
def test_d2_denials_and_valid_boundary(logical_id):
    document = read()["Resources"][logical_id]["Properties"]["PolicyDocument"]
    for action in ACTIONS:
        if "PolicyVersion" in action or action == "iam:DeletePolicy":
            resources = [EXECUTION, BOUNDARY]
        elif action in {"iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile"}:
            resources = [PREFIX + "user/probe"]
        elif action == "iam:CreateSAMLProvider":
            resources = [PREFIX + "saml-provider/probe"]
        elif action == "iam:CreateOpenIDConnectProvider":
            resources = [PREFIX + "oidc-provider/example.com"]
        else:
            resources = [ROLE]
        context = {"iam:PermissionsBoundary": PREFIX + "policy/other"} if action == "iam:PutRolePermissionsBoundary" else {}
        for resource in resources:
            assert decision(document, action, resource, context) == "Deny", (action, resource)
    for action in ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]:
        assert decision(document, action, ROLE, {"iam:PermissionsBoundary": PREFIX + "policy/other"}) == "Deny"
        assert decision(document, action, ROLE, {"iam:PermissionsBoundary": BOUNDARY}) == "Allow"


def test_d2_service_linked_role_cannot_be_created_without_boundary():
    # AWS::IAM::ServiceLinkedRole uses this separate IAM API, which has no boundary parameter.
    resource = PREFIX + "role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
    context = {"iam:AWSServiceName": "autoscaling.amazonaws.com"}
    decisions = {
        logical_id: decision(value["Properties"]["PolicyDocument"], "iam:CreateServiceLinkedRole", resource, context)
        for logical_id, value in read()["Resources"].items()
    }
    assert "Deny" in decisions.values(), decisions
