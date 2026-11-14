import json
import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("change", [
    "required", "bypass", "enforcement", "pull_request", "non_fast_forward", "deletion",
])
def test_other_guarantees_are_enforced(change, tmp_path, monkeypatch):
    module = runpy.run_path("tests/policy/test_ruleset.py")
    ruleset = json.loads(Path("setup/pr-only-main.json").read_text())
    if change == "required":
        rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")
        rule["parameters"]["required_status_checks"][0]["context"] = "different"
    elif change == "bypass":
        ruleset["bypass_actors"] = [
            {"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"}
        ]
    elif change == "enforcement":
        ruleset["enforcement"] = "disabled"
    else:
        ruleset["rules"] = [r for r in ruleset["rules"] if r["type"] != change]
    assert module["violations"](ruleset)
    candidate = tmp_path / "ruleset.json"
    candidate.write_text(json.dumps(ruleset))
    invariant = module["test_the_ruleset_definition_holds"]
    monkeypatch.setitem(invariant.__globals__, "RULESET", candidate)
    with pytest.raises(AssertionError):
        invariant()
