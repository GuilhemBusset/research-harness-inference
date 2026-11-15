"""The invariant checker must reject each denial missing from the real parsed template."""

import copy
import importlib.util
from pathlib import Path

from cfnlint.decode import decode_str


def test_every_required_denial_is_checked():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("bootstrap_invariant", root / "tests/policy/test_bootstrap_policies.py")
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    template, errors = decode_str((root / "setup/bootstrap/policies.yaml").read_text())
    assert not errors
    assert isinstance(template, dict)
    assert checker.violations(template) == []
    required = {
        "iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:CreateSAMLProvider",
        "iam:CreateOpenIDConnectProvider", "iam:CreateRole", "iam:PutRolePermissionsBoundary",
        "iam:DeleteRolePermissionsBoundary", "iam:CreatePolicyVersion", "iam:DeletePolicyVersion",
        "iam:SetDefaultPolicyVersion", "iam:DeletePolicy",
    }
    for logical_id in ["ExecutionPolicy", "PermissionsBoundary"]:
        for action in required:
            changed = copy.deepcopy(template)
            statements = changed["Resources"][logical_id]["Properties"]["PolicyDocument"]["Statement"]
            for statement in statements:
                if statement.get("Effect") == "Deny":
                    actions = statement.get("Action", [])
                    actions = actions if isinstance(actions, list) else [actions]
                    statement["Action"] = [candidate for candidate in actions if candidate != action]
            assert checker.violations(changed), (logical_id, action)
