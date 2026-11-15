"""An intrinsic resource attribute can itself contain the global IAM wildcard."""

import importlib.util
from pathlib import Path

import pytest


def load_check(filename):
    path = Path(__file__).resolve().parents[1] / "policy" / filename
    spec = importlib.util.spec_from_file_location("probe31_value_" + path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_policy = load_check("conftest.py")
cdk = _policy.cdk


@pytest.mark.parametrize("holder", ["policy", "managed", "inline"])
def test_attribute_holding_every_resource_is_not_narrowed(cdk, holder):
    stack = cdk.Stack()
    scope = cdk.aws_ssm.CfnParameter(stack, "Scope", type="String", value="*")
    document = {
        "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": scope.attr_value}]
    }
    if holder == "policy":
        cdk.aws_iam.CfnPolicy(
            stack, "Identity", policy_name="Example", policy_document=document, roles=["Example"]
        )
    elif holder == "managed":
        cdk.aws_iam.CfnManagedPolicy(stack, "Identity", policy_document=document)
    else:
        cdk.aws_iam.CfnRole(
            stack,
            "Identity",
            assume_role_policy_document={
                "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                               "Principal": {"Service": "ec2.amazonaws.com"}}]
            },
            policies=[cdk.aws_iam.CfnRole.PolicyProperty(policy_name="Example", policy_document=document)],
        )
    template = cdk.assertions.Template.from_stack(stack).to_json()
    parameter_id = stack.get_logical_id(scope)
    assert template["Resources"][parameter_id]["Properties"]["Value"] == "*"
    modules = {("built.template.json", name): "stage" for name in template["Resources"]}
    findings = load_check("test_iam_wildcards.py").violations(
        {"built.template.json": template}, modules, {}
    )
    assert findings, "GetAtt(Scope, Value) resolves to '*'; the action needs its module's acknowledgement"
