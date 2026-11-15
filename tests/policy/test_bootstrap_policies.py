"""The bounded bootstrap keeps its denials (system design, section 8.4, control 1; evidence E20).

A merge to `main` is deployment rights in the account. CloudFormation deploys a merged template
with the execution policy, and every role a deployment creates carries the permissions boundary,
so each of the two policies of `setup/bootstrap/policies.yaml` must hold every denial alone:
creating IAM users, access keys and login profiles; creating SAML and OpenID Connect providers;
creating a role without the boundary; removing or replacing a role's boundary; changing or
deleting either policy.

This invariant reads that template, not a synthesized one. Trust to another account is not here:
no IAM condition key reads a role's trust policy, so `test_trust.py` holds it over the templates.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from cfnlint.decode import decode_str

TEMPLATE = Path(__file__).resolve().parents[2] / "setup" / "bootstrap" / "policies.yaml"

# The two managed policies, by logical ID, and the fixed name of each: the bootstrap command names
# them, and a policy names its own ARN by it.
EXECUTION = "ExecutionPolicy"
BOUNDARY = "PermissionsBoundary"
POLICIES = {EXECUTION: "rhi-cfn-execution", BOUNDARY: "rhi-permissions-boundary"}


def arn(name: str) -> dict[str, str]:
    """The ARN of a managed policy of the deploying account, as the template writes it."""
    return {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:policy/" + name}


EVERYTHING = ["*"]
THE_TWO_POLICIES = [arn(name) for name in POLICIES.values()]
# True of a request that names another boundary, and of one that names none.
NOT_THE_BOUNDARY = {"StringNotEquals": {"iam:PermissionsBoundary": arn(POLICIES[BOUNDARY])}}

# Every denial: the action, the resources it must cover, and the one condition it may carry.
DENIALS: dict[str, tuple[list[Any], dict[str, Any] | None]] = {
    "iam:CreateUser": (EVERYTHING, None),
    "iam:CreateAccessKey": (EVERYTHING, None),
    "iam:CreateLoginProfile": (EVERYTHING, None),
    "iam:CreateSAMLProvider": (EVERYTHING, None),
    "iam:CreateOpenIDConnectProvider": (EVERYTHING, None),
    "iam:CreateRole": (EVERYTHING, NOT_THE_BOUNDARY),
    "iam:PutRolePermissionsBoundary": (EVERYTHING, NOT_THE_BOUNDARY),
    "iam:DeleteRolePermissionsBoundary": (EVERYTHING, None),
    "iam:CreatePolicyVersion": (THE_TWO_POLICIES, None),
    "iam:DeletePolicyVersion": (THE_TWO_POLICIES, None),
    "iam:SetDefaultPolicyVersion": (THE_TWO_POLICIES, None),
    "iam:DeletePolicy": (THE_TWO_POLICIES, None),
}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def denies(document: dict[str, Any], action: str, resource: Any, condition: dict[str, Any] | None) -> bool:
    """Whether a statement of the policy denies the action on the resource, under that condition
    and no other. The action is named in full under `Action`: a `NotAction` proves nothing here."""
    return any(
        statement.get("Effect") == "Deny"
        and action in as_list(statement.get("Action", []))
        and (resource in as_list(statement.get("Resource", [])) or "*" in as_list(statement.get("Resource", [])))
        and statement.get("Condition") == condition
        for statement in as_list(document.get("Statement", []))
    )


def violations(template: dict[str, Any]) -> list[str]:
    """What the template lacks to be the bounded bootstrap's two policies; empty when it holds."""
    found = []
    for logical_id, name in POLICIES.items():
        resource = template.get("Resources", {}).get(logical_id, {})
        properties = resource.get("Properties", {})
        if (
            resource.get("Type") != "AWS::IAM::ManagedPolicy"
            or properties.get("ManagedPolicyName") != name
            # A path would change the ARN the denials and the bootstrap name the policy by.
            or properties.get("Path", "/") != "/"
        ):
            found.append(f"{logical_id} is not the managed policy {name}")
            continue
        document = properties.get("PolicyDocument", {})
        found.extend(
            f"{name} does not deny {action}"
            for action, (resources, condition) in DENIALS.items()
            if not all(denies(document, action, resource, condition) for resource in resources)
        )
    return found


def read() -> dict[str, Any]:
    """The template, parsed by cfn-lint's reader, which knows `!Sub`, into plain dictionaries."""
    content, unreadable = decode_str(TEMPLATE.read_text(encoding="utf-8"))
    assert not unreadable
    return json.loads(json.dumps(content))


def deny_statements(template: dict[str, Any], logical_id: str) -> list[dict[str, Any]]:
    statements = template["Resources"][logical_id]["Properties"]["PolicyDocument"]["Statement"]
    return [statement for statement in statements if statement["Effect"] == "Deny"]


def test_the_two_policies_hold_every_denial():
    assert violations(read()) == []


@pytest.mark.parametrize("action", DENIALS)
@pytest.mark.parametrize("logical_id", POLICIES)
def test_fails_when_a_denial_is_removed(logical_id: str, action: str):
    template = read()
    for statement in deny_statements(template, logical_id):
        statement["Action"] = [denied for denied in as_list(statement["Action"]) if denied != action]
    assert violations(template) == [f"{POLICIES[logical_id]} does not deny {action}"]


@pytest.mark.parametrize("unprotected", THE_TWO_POLICIES)
@pytest.mark.parametrize("logical_id", POLICIES)
def test_fails_when_either_policy_can_be_changed(logical_id: str, unprotected: dict[str, str]):
    template = read()
    for statement in deny_statements(template, logical_id):
        statement["Resource"] = [resource for resource in as_list(statement["Resource"]) if resource != unprotected]
    expected = [
        f"{POLICIES[logical_id]} does not deny {action}" for action, on in DENIALS.items() if unprotected in on[0]
    ]
    assert len(expected) == 4
    assert violations(template) == expected


@pytest.mark.parametrize("logical_id", POLICIES)
def test_fails_when_another_boundary_is_accepted(logical_id: str):
    expected = [f"{POLICIES[logical_id]} does not deny {action}" for action in DENIALS if DENIALS[action][1]]
    assert len(expected) == 2
    for accepted in (
        {"StringNotLike": {"iam:PermissionsBoundary": "*"}},
        {"StringNotEquals": {"iam:PermissionsBoundary": arn("another")}},
    ):
        template = read()
        for statement in deny_statements(template, logical_id):
            if "Condition" in statement:
                statement["Condition"] = copy.deepcopy(accepted)
        assert violations(template) == expected


def test_fails_when_a_denial_is_made_conditional_or_an_allowance():
    template = read()
    for statement in deny_statements(template, EXECUTION):
        if "iam:CreateUser" in as_list(statement["Action"]):
            statement["Condition"] = {"StringEquals": {"aws:RequestedRegion": "us-east-1"}}
    for statement in deny_statements(template, BOUNDARY):
        if "iam:CreateSAMLProvider" in as_list(statement["Action"]):
            statement["Effect"] = "Allow"
    assert violations(template) == [
        "rhi-cfn-execution does not deny iam:CreateUser",
        "rhi-cfn-execution does not deny iam:CreateAccessKey",
        "rhi-cfn-execution does not deny iam:CreateLoginProfile",
        "rhi-permissions-boundary does not deny iam:CreateSAMLProvider",
        "rhi-permissions-boundary does not deny iam:CreateOpenIDConnectProvider",
    ]


@pytest.mark.parametrize("logical_id", POLICIES)
def test_fails_when_a_policy_has_another_name_or_is_missing(logical_id: str):
    expected = [f"{logical_id} is not the managed policy {POLICIES[logical_id]}"]
    renamed = read()
    renamed["Resources"][logical_id]["Properties"]["ManagedPolicyName"] = {"Fn::Sub": "${AWS::StackName}-policy"}
    assert violations(renamed) == expected
    on_a_path = read()
    on_a_path["Resources"][logical_id]["Properties"]["Path"] = "/rhi/"
    assert violations(on_a_path) == expected
    missing = read()
    del missing["Resources"][logical_id]
    assert violations(missing) == expected
