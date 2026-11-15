"""Run the actual acceptance test command against isolated copies with single denials removed."""

import copy
import json
import subprocess
import sys
from pathlib import Path

from cfnlint.decode import decode_str


def test_the_command_rejects_every_removed_denial(tmp_path):
    root = Path(__file__).resolve().parents[2]
    target = tmp_path / "setup/bootstrap/policies.yaml"
    target.parent.mkdir(parents=True)
    test_file = tmp_path / "tests/policy/test_bootstrap_policies.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text((root / "tests/policy/test_bootstrap_policies.py").read_text())
    original, errors = decode_str((root / "setup/bootstrap/policies.yaml").read_text())
    assert not errors
    assert isinstance(original, dict)
    command = [sys.executable, "-m", "pytest", "-q", "--confcutdir", str(tmp_path), str(test_file)]
    target.write_text(json.dumps(original))
    baseline = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    for logical_id, resource in original["Resources"].items():
        actions = {
            action
            for statement in resource["Properties"]["PolicyDocument"]["Statement"]
            if statement["Effect"] == "Deny"
            for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
        }
        assert actions
        for action in actions:
            changed = copy.deepcopy(original)
            for statement in changed["Resources"][logical_id]["Properties"]["PolicyDocument"]["Statement"]:
                if statement["Effect"] == "Deny":
                    existing = statement["Action"]
                    existing = existing if isinstance(existing, list) else [existing]
                    statement["Action"] = [candidate for candidate in existing if candidate != action]
            target.write_text(json.dumps(changed))
            result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)
            assert result.returncode == 1, (logical_id, action, result.stdout, result.stderr)
            assert "FAILED tests/policy/test_bootstrap_policies.py::test_the_two_policies_hold_every_denial" in result.stdout
