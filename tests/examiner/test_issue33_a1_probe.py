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
def test_real_test_fails_after_denial_removal(logical_id, action, tmp_path, monkeypatch):
    checker = load_checker()
    checker.test_the_two_policies_hold_every_denial()
    template, errors = decode_str((ROOT / "setup/bootstrap/policies.yaml").read_text())
    assert not errors
    template = json.loads(json.dumps(template))
    for statement in template["Resources"][logical_id]["Properties"]["PolicyDocument"]["Statement"]:
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        if statement["Effect"] == "Deny":
            statement["Action"] = [value for value in actions if value != action]
    mutated = tmp_path / "policies.json"
    mutated.write_text(json.dumps(template))
    monkeypatch.setattr(checker, "TEMPLATE", mutated)
    with pytest.raises(AssertionError):
        checker.test_the_two_policies_hold_every_denial()
