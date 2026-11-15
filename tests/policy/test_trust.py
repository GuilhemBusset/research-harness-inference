"""No identity provider, no long-lived identity and no trust to another account (system design,
section 8.4, control 2).

A merge to `main` is deployment rights in the account. What a merge deploys may not create a way
into the account that outlives it or comes from outside it: an IAM user, its access key or its
console password, a SAML or OpenID Connect provider, or a role that another account can assume.
The last one has no other control: no IAM condition key reads a role's trust policy, so the
bounded bootstrap cannot deny it.

A template is environment-agnostic, so the only way it can name the deploying account is the
`AWS::AccountId` pseudo parameter. A principal built from it is the same account, and so is a role
of the same template. A service is not an account. Any other principal is refused.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

FORBIDDEN = {
    "AWS::IAM::User": "an IAM user",
    "AWS::IAM::AccessKey": "an access key",
    "AWS::IAM::SAMLProvider": "a SAML provider",
    "AWS::IAM::OIDCProvider": "an OpenID Connect provider",
    # What the CDK's older construct for an OpenID Connect provider creates it with.
    "Custom::AWSCDKOpenIdConnectProvider": "an OpenID Connect provider",
}
LOGIN_PROFILE = "a login profile"
ANOTHER_ACCOUNT = "its trust policy names a principal that is neither a service nor the deploying account"

# A principal of the deploying account, as `Fn::Sub` would write it: the account alone, or an ARN
# of IAM or STS whose account field is the pseudo parameter.
DEPLOYING_ACCOUNT = re.compile(
    r"\$\{AWS::AccountId\}|arn:(aws[a-z-]*|\$\{AWS::Partition\}):(iam|sts)::\$\{AWS::AccountId\}:.+"
)


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def written(value: Any) -> str | None:
    """A value of a template as `Fn::Sub` would write it, with pseudo parameters as `${AWS::...}`.
    None when it is built from anything but text and pseudo parameters."""
    if isinstance(value, str):
        return None if "$" in value else value
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((function, argument),) = value.items()
    if function == "Ref" and isinstance(argument, str) and argument.startswith("AWS::"):
        return "${" + argument + "}"
    if function == "Fn::Sub" and isinstance(argument, str):
        return argument
    if function == "Fn::Join" and isinstance(argument, list) and len(argument) == 2 and isinstance(argument[1], list):
        parts = [written(part) for part in argument[1]]
        if isinstance(argument[0], str) and all(isinstance(part, str) for part in parts):
            return argument[0].join(part for part in parts if part is not None)
    return None


def is_the_deploying_account(principal: Any, template: Mapping[str, Any]) -> bool:
    """Whether a value under `AWS` in a principal can only be of the account that deploys."""
    if isinstance(principal, dict) and list(principal) == ["Fn::GetAtt"]:
        # The ARN of a role of the same template.
        target = principal["Fn::GetAtt"]
        return (
            isinstance(target, list)
            and len(target) == 2
            and target[1] == "Arn"
            and template.get("Resources", {}).get(target[0], {}).get("Type") == "AWS::IAM::Role"
        )
    text = written(principal)
    return text is not None and DEPLOYING_ACCOUNT.fullmatch(text) is not None


def trusts_only_the_deploying_account(statement: Any, template: Mapping[str, Any]) -> bool:
    """Whether one statement of a trust policy lets in services and the deploying account only."""
    if not isinstance(statement, dict):
        return False
    if statement.get("Effect") == "Deny":
        return True
    principal = statement.get("Principal")
    # A principal that is not a mapping is `*`, anyone, or is missing (`NotPrincipal`).
    return isinstance(principal, dict) and all(
        kind == "Service"
        or (kind == "AWS" and all(is_the_deploying_account(value, template) for value in as_list(values)))
        for kind, values in principal.items()
    )


def violations(templates: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    """Every forbidden resource of the templates, and every role another account can assume, as
    `(template path, logical ID, what it is)`."""
    found = []
    for path, template in templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            kind = resource.get("Type")
            properties = resource.get("Properties", {})
            if kind in FORBIDDEN:
                found.append((path, logical_id, FORBIDDEN[kind]))
            if kind == "AWS::IAM::User" and "LoginProfile" in properties:
                found.append((path, logical_id, LOGIN_PROFILE))
            if kind != "AWS::IAM::Role":
                continue
            document = properties.get("AssumeRolePolicyDocument")
            trust = as_list(document["Statement"]) if isinstance(document, dict) and "Statement" in document else [None]
            if not all(trusts_only_the_deploying_account(statement, template) for statement in trust):
                found.append((path, logical_id, ANOTHER_ACCOUNT))
    return found


def test_the_app_creates_no_way_in(templates: dict[str, dict[str, Any]]) -> None:
    assert violations(templates) == []


def found_in(resource: dict[str, Any], **others: dict[str, Any]) -> list[str]:
    """What is refused in a template, built in the test, that holds the resource."""
    return [what for _, _, what in violations({"built.template.json": {"Resources": {"Built": resource} | others}})]


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        pytest.param({"Type": "AWS::IAM::User"}, [FORBIDDEN["AWS::IAM::User"]], id="a user"),
        pytest.param(
            {"Type": "AWS::IAM::AccessKey", "Properties": {"UserName": "someone"}},
            [FORBIDDEN["AWS::IAM::AccessKey"]],
            id="an access key",
        ),
        pytest.param(
            {"Type": "AWS::IAM::User", "Properties": {"LoginProfile": {"Password": {"Ref": "Password"}}}},
            [FORBIDDEN["AWS::IAM::User"], LOGIN_PROFILE],
            id="a login profile",
        ),
        pytest.param(
            {"Type": "AWS::IAM::SAMLProvider", "Properties": {"SamlMetadataDocument": "<EntityDescriptor/>"}},
            [FORBIDDEN["AWS::IAM::SAMLProvider"]],
            id="a SAML provider",
        ),
        pytest.param(
            {"Type": "AWS::IAM::OIDCProvider", "Properties": {"Url": "https://example.com"}},
            [FORBIDDEN["AWS::IAM::OIDCProvider"]],
            id="an OpenID Connect provider",
        ),
        pytest.param(
            {"Type": "Custom::AWSCDKOpenIdConnectProvider", "Properties": {"Url": "https://example.com"}},
            [FORBIDDEN["AWS::IAM::OIDCProvider"]],
            id="an OpenID Connect provider made by a custom resource",
        ),
    ],
)
def test_detects_each_forbidden_resource(resource: dict[str, Any], expected: list[str]) -> None:
    assert found_in(resource) == expected


def role(*principals: Any) -> dict[str, Any]:
    """A role, built in the test, with one statement of its trust policy for each principal."""
    statements = [{"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": principal} for principal in principals]
    return {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Statement": statements}}}


def test_the_deploying_account_is_not_another(cdk: ModuleType) -> None:
    def built(principal: Any) -> dict[str, Any]:
        """The template of a stack that holds one role, which the principal can assume."""
        stack = cdk.Stack()
        cdk.aws_iam.Role(stack, "Role", assumed_by=principal)
        return {"built.template.json": cdk.assertions.Template.from_stack(stack).to_json()}

    # The CDK writes the first with the pseudo parameter and the second with the account's ID.
    assert violations(built(cdk.aws_iam.AccountRootPrincipal())) == []
    assert [what for _, _, what in violations(built(cdk.aws_iam.AccountPrincipal("123456789012")))] == [ANOTHER_ACCOUNT]

    # The same by hand, as an ARN and as the account alone.
    root = ["arn:", {"Ref": "AWS::Partition"}, ":iam::", {"Ref": "AWS::AccountId"}, ":root"]
    assert found_in(role({"AWS": {"Fn::Join": ["", root]}}, {"AWS": {"Ref": "AWS::AccountId"}})) == []
    another = [*root[:3], "123456789012", ":root"]
    assert found_in(role({"AWS": {"Fn::Join": ["", another]}})) == [ANOTHER_ACCOUNT]
    assert found_in(role({"AWS": "123456789012"})) == [ANOTHER_ACCOUNT]


def test_a_service_and_a_role_of_the_template_are_not_another_account() -> None:
    services = {"Service": ["ec2.amazonaws.com", {"Fn::Join": ["", ["codebuild.", {"Ref": "AWS::URLSuffix"}]]}]}
    other = {"Type": "AWS::IAM::Role", "Properties": {"AssumeRolePolicyDocument": {"Statement": []}}}
    assert found_in(role(services, {"AWS": {"Fn::GetAtt": ["Other", "Arn"]}}), Other=other) == []


@pytest.mark.parametrize(
    "principal",
    [
        pytest.param("*", id="anyone"),
        pytest.param({"AWS": "*"}, id="any account"),
        pytest.param(
            {"AWS": [{"Ref": "AWS::AccountId"}, "111122223333"]}, id="another account beside the deploying one"
        ),
        pytest.param({"AWS": {"Ref": "TrustedAccount"}}, id="an account given by a parameter"),
        pytest.param({"AWS": {"Fn::Sub": "arn:aws:iam::${TrustedAccount}:root"}}, id="an ARN built from a parameter"),
        pytest.param({"AWS": {"Fn::GetAtt": ["Bucket", "Arn"]}}, id="what is not a role of the template"),
        pytest.param({"Service": "ec2.amazonaws.com", "AWS": "111122223333"}, id="another account beside a service"),
        pytest.param({"Federated": "accounts.google.com"}, id="an identity provider"),
    ],
)
def test_refuses_a_principal_from_outside_the_account(principal: Any) -> None:
    assert found_in(role({"Service": "ec2.amazonaws.com"}, principal)) == [ANOTHER_ACCOUNT]
