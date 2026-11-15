"""Independent forbidden-resource and trust checks over synthesized templates."""

import importlib.util
from pathlib import Path


def load_check(filename):
    spec = importlib.util.spec_from_file_location(
        "issue31_" + filename.removesuffix(".py"), Path(__file__).resolve().parents[1] / "policy" / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


import pytest


@pytest.mark.parametrize(
    "kind", ["AWS::IAM::User", "AWS::IAM::AccessKey", "AWS::IAM::SAMLProvider", "AWS::IAM::OIDCProvider"]
)
def test_forbidden_resource_is_found(cdk, kind):
    stack = cdk.Stack()
    cdk.CfnResource(stack, "Forbidden", type=kind)
    template = cdk.assertions.Template.from_stack(stack).to_json()
    assert load_check("test_trust.py").violations({"built.template.json": template})


def test_login_profile_is_reported(cdk):
    stack = cdk.Stack()
    password = cdk.CfnParameter(stack, "InitialPassword", no_echo=True)
    cdk.aws_iam.CfnUser(
        stack, "User", login_profile=cdk.aws_iam.CfnUser.LoginProfileProperty(password=password.value_as_string)
    )
    template = cdk.assertions.Template.from_stack(stack).to_json()
    findings = load_check("test_trust.py").violations({"built.template.json": template})
    assert len(findings) == 2


def test_literal_foreign_account_in_trust_is_found(cdk):
    stack = cdk.Stack()
    cdk.aws_iam.Role(stack, "Role", assumed_by=cdk.aws_iam.AccountPrincipal("123456789012"))
    template = cdk.assertions.Template.from_stack(stack).to_json()
    assert load_check("test_trust.py").violations({"built.template.json": template})


_policy = load_check("conftest.py")
cdk = _policy.cdk
read_app = _policy.read_app
read_acknowledged = _policy.read_acknowledged
synthesized = _policy.synthesized
templates = _policy.templates
created_by = _policy.created_by
module_names = _policy.module_names
