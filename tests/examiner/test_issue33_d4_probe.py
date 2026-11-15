"""Exercise the actual bootstrap policy checker on the real template."""
import importlib.util
import json
from pathlib import Path

import pytest
from cfnlint.decode import decode_str

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = [
    "iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile",
    "iam:CreateSAMLProvider", "iam:CreateOpenIDConnectProvider", "iam:CreateRole",
    "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary",
    "iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:DeletePolicy",
]


def load_checker():
    spec = importlib.util.spec_from_file_location("bootstrap_checker", ROOT / "tests/policy/test_bootstrap_policies.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("logical_id", ["ExecutionPolicy", "PermissionsBoundary"])
@pytest.mark.parametrize("action", ACTIONS)
def test_parsed_denial_is_asserted(logical_id, action):
    checker = load_checker()
    template, errors = decode_str((ROOT / "setup/bootstrap/policies.yaml").read_text())
    assert not errors
    template = json.loads(json.dumps(template))
    assert checker.violations(template) == []
    for statement in template["Resources"][logical_id]["Properties"]["PolicyDocument"]["Statement"]:
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        if statement["Effect"] == "Deny":
            statement["Action"] = [value for value in actions if value != action]
    assert any(action in violation for violation in checker.violations(template))
