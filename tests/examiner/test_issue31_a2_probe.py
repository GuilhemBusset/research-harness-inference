"""Independent mutations for each forbidden identity resource and IAM wildcard."""

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


@pytest.mark.parametrize("holder", ["AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::Role"])
@pytest.mark.parametrize("action,resource", [("*", "arn:aws:s3:::example-bucket/*"), ("s3:GetObject", "*")])
def test_each_identity_policy_rejects_wildcards(cdk, holder, action, resource):
    stack = cdk.Stack()
    document = {"Statement": [{"Effect": "Allow", "Action": action, "Resource": resource}]}
    properties = {"PolicyDocument": document}
    if holder == "AWS::IAM::Role":
        properties = {
            "AssumeRolePolicyDocument": {"Statement": []},
            "Policies": [{"PolicyName": "Example", "PolicyDocument": document}],
        }
    cdk.CfnResource(stack, "Identity", type=holder, properties=properties)
    template = cdk.assertions.Template.from_stack(stack).to_json()
    modules = {("built.template.json", name): "stage" for name in template["Resources"]}
    assert load_check("test_iam_wildcards.py").violations({"built.template.json": template}, modules, {})


@pytest.mark.parametrize(
    "kind,profile",
    [
        ("AWS::IAM::User", False),
        ("AWS::IAM::AccessKey", False),
        ("AWS::IAM::SAMLProvider", False),
        ("AWS::IAM::OIDCProvider", False),
        ("AWS::IAM::User", True),
    ],
)
def test_each_forbidden_resource_is_detected(cdk, kind, profile):
    stack = cdk.Stack()
    properties = {}
    if profile:
        password = cdk.CfnParameter(stack, "InitialPassword", no_echo=True)
        properties = {"LoginProfile": {"Password": password.value_as_string}}
    cdk.CfnResource(stack, "Forbidden", type=kind, properties=properties)
    template = cdk.assertions.Template.from_stack(stack).to_json()
    found = load_check("test_trust.py").violations({"built.template.json": template})
    assert len(found) == (2 if profile else 1)


_policy = load_check("conftest.py")
cdk = _policy.cdk
read_app = _policy.read_app
read_acknowledged = _policy.read_acknowledged
synthesized = _policy.synthesized
templates = _policy.templates
created_by = _policy.created_by
module_names = _policy.module_names
