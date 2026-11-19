"""The admin's sign-in in the access stack (system design, section 7.4): a user pool nobody signs up
to, with MFA required and one user, kept on deletion and on replacement; its sign-in domain, whose
prefix no template holds; and the load balancer's client.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from rhi import config

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = config.load({"RHI_CONFIG": "example"})
RECORD = REPO / "tests" / "policy" / "stateful_logical_ids.json"
STACK_NAME = "Rhi-Access"

# The unique identifier that ends a stack's ID, `arn:...:stack/<name>/<identifier>`.
FROM_THE_STACK_ID = {"Fn::Select": [2, {"Fn::Split": ["/", {"Ref": "AWS::StackId"}]}]}


def access_template(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    (template,) = [template for path, template in templates.items() if Path(path).name.startswith("RhiAccess")]
    return template


def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {name: resource for name, resource in template["Resources"].items() if resource["Type"] == kind}


def the_one(template: dict[str, Any], kind: str) -> tuple[str, dict[str, Any]]:
    """The logical ID and the properties of the template's only resource of the type."""
    ((logical_id, resource),) = of_type(template, kind).items()
    return logical_id, resource["Properties"]


@pytest.fixture(scope="module")
def access(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return access_template(templates)


@pytest.fixture(scope="module")
def another_configuration(app_module: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The access stack's template for another admin and another domain, with the HTTPS listener
    left off."""
    outdir = tmp_path_factory.mktemp("another")
    context = json.loads((REPO / "cdk.json").read_text(encoding="utf-8"))["context"] | {"aws:cdk:bundling-stacks": []}
    other = dataclasses.replace(
        EXAMPLE, admin_email="someone@example.org", domain="endpoint.example.org", https_listener=False
    )
    app_module.build(other, outdir=str(outdir), context=context).synth()
    return access_template(
        {path.as_posix(): json.loads(path.read_text(encoding="utf-8")) for path in outdir.rglob("*.template.json")}
    )


def test_nobody_signs_up_and_mfa_with_an_authenticator_app_is_required(access: dict[str, Any]) -> None:
    _, pool = the_one(access, "AWS::Cognito::UserPool")
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert pool["MfaConfiguration"] == "ON"
    # The only second factor: no text message, so no role that sends one and no phone number.
    assert pool["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]
    assert "SmsConfiguration" not in pool


def test_the_one_user_is_the_configured_admin(access: dict[str, Any], another_configuration: dict[str, Any]) -> None:
    for template, address in ((access, EXAMPLE.admin_email), (another_configuration, "someone@example.org")):
        pool_id, _ = the_one(template, "AWS::Cognito::UserPool")
        _, user = the_one(template, "AWS::Cognito::UserPoolUser")
        assert user["UserPoolId"] == {"Ref": pool_id}
        assert user["Username"] == address
        assert {"Name": "email", "Value": address} in user["UserAttributes"]


def test_the_user_pool_is_kept_and_its_logical_id_is_recorded(access: dict[str, Any]) -> None:
    ((pool_id, pool),) = of_type(access, "AWS::Cognito::UserPool").items()
    assert pool["Properties"]["DeletionProtection"] == "ACTIVE"
    assert pool["DeletionPolicy"] == "Retain"
    assert pool["UpdateReplacePolicy"] == "Retain"
    assert pool_id in json.loads(RECORD.read_text(encoding="utf-8"))[STACK_NAME]


def test_the_admins_account_is_kept_with_its_user_pool(access: dict[str, Any]) -> None:
    # The user is a resource of its own: the user pool's policy does not keep it.
    ((_, user),) = of_type(access, "AWS::Cognito::UserPoolUser").items()
    assert user["DeletionPolicy"] == "Retain"
    assert user["UpdateReplacePolicy"] == "Retain"


def test_the_domain_prefix_is_computed_at_deployment(
    access: dict[str, Any], templates: dict[str, dict[str, Any]]
) -> None:
    pool_id, _ = the_one(access, "AWS::Cognito::UserPool")
    _, domain = the_one(access, "AWS::Cognito::UserPoolDomain")
    assert domain["UserPoolId"] == {"Ref": pool_id}
    assert domain["Domain"] == FROM_THE_STACK_ID
    assert "CustomDomainConfig" not in domain
    # No template of the cloud assembly gives a sign-in domain a prefix it holds.
    prefixes = [
        resource["Properties"]["Domain"]
        for template in templates.values()
        for resource in of_type(template, "AWS::Cognito::UserPoolDomain").values()
    ]
    assert prefixes == [FROM_THE_STACK_ID]


def test_the_load_balancers_client_has_a_generated_secret_the_code_flow_and_one_callback(
    access: dict[str, Any], another_configuration: dict[str, Any]
) -> None:
    for template, domain in ((access, EXAMPLE.domain), (another_configuration, "endpoint.example.org")):
        pool_id, _ = the_one(template, "AWS::Cognito::UserPool")
        _, client = the_one(template, "AWS::Cognito::UserPoolClient")
        assert client["UserPoolId"] == {"Ref": pool_id}
        assert client["GenerateSecret"] is True
        assert client["AllowedOAuthFlowsUserPoolClient"] is True
        assert client["AllowedOAuthFlows"] == ["code"]
        assert client["AllowedOAuthScopes"] == ["openid"]
        assert client["CallbackURLs"] == [f"https://{domain}/oauth2/idpresponse"]
