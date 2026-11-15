"""Independent same-account pseudo parameter versus literal-account trust."""

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


def test_pseudo_account_is_admitted_but_literal_account_is_not(cdk):
    check = load_check("test_trust.py")
    results = []
    for account in ({"Ref": "AWS::AccountId"}, "123456789012"):
        stack = cdk.Stack()
        cdk.aws_iam.CfnRole(
            stack,
            "Role",
            assume_role_policy_document={
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "sts:AssumeRole",
                        "Principal": {"AWS": account},
                    }
                ]
            },
        )
        template = cdk.assertions.Template.from_stack(stack).to_json()
        results.append(check.violations({"built.template.json": template}))
    assert results[0] == []
    assert len(results[1]) == 1


_policy = load_check("conftest.py")
cdk = _policy.cdk
read_app = _policy.read_app
read_acknowledged = _policy.read_acknowledged
synthesized = _policy.synthesized
templates = _policy.templates
created_by = _policy.created_by
module_names = _policy.module_names
