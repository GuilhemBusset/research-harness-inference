"""Independent check that conditional wildcard resources remain forbidden (D4)."""

import importlib.util
from pathlib import Path

import pytest


def implementation():
    path = Path(__file__).resolve().parents[1] / "policy" / "test_iam_wildcards.py"
    spec = importlib.util.spec_from_file_location("issue31_wildcards", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("holder", ["policy", "managed", "inline"])
def test_conditional_wildcard_resource_is_rejected(cdk, holder):
    check = implementation()
    stack = cdk.Stack()
    switch = cdk.CfnParameter(stack, "Mode", type="String", default="wide")
    condition = cdk.CfnCondition(stack, "Wide", expression=cdk.Fn.condition_equals(switch.value_as_string, "wide"))
    resource = cdk.Fn.condition_if(condition.logical_id, "*", "arn:aws:s3:::example-bucket/*")
    document = {"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": resource}]}
    if holder == "policy":
        cdk.aws_iam.CfnPolicy(stack, "Identity", policy_name="Example", policy_document=document, roles=["Example"])
    elif holder == "managed":
        cdk.aws_iam.CfnManagedPolicy(stack, "Identity", policy_document=document)
    else:
        cdk.aws_iam.CfnRole(
            stack,
            "Identity",
            assume_role_policy_document={
                "Statement": [
                    {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "ec2.amazonaws.com"}}
                ]
            },
            policies=[cdk.aws_iam.CfnRole.PolicyProperty(policy_name="Example", policy_document=document)],
        )
    template = cdk.assertions.Template.from_stack(stack).to_json()
    modules = {("built.template.json", name): "stage" for name in template["Resources"]}
    findings = check.violations({"built.template.json": template}, modules, {})
    assert findings, "An unacknowledged action can grant access to every resource when Mode is wide"


def load_check(filename):
    spec = importlib.util.spec_from_file_location(
        "issue31_fixture", Path(__file__).resolve().parents[1] / "policy" / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_policy = load_check("conftest.py")
cdk = _policy.cdk
read_app = _policy.read_app
read_acknowledged = _policy.read_acknowledged
synthesized = _policy.synthesized
templates = _policy.templates
created_by = _policy.created_by
module_names = _policy.module_names
