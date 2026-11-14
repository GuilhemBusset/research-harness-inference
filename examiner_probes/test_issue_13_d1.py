import copy
import json
import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("source", ["absent", None, 42])
@pytest.mark.parametrize("location", ["required", "additional-rule"])
def test_sources_are_enforced(source, location, tmp_path, monkeypatch):
    module = runpy.run_path("tests/policy/test_ruleset.py")
    ruleset = json.loads(Path("setup/pr-only-main.json").read_text())
    assert module["violations"](ruleset) == []
    check = {"context": "required"}
    if source != "absent":
        check["integration_id"] = source
    if location == "required":
        rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")
        rule["parameters"]["required_status_checks"] = [check]
    else:
        check["context"] = "additional"
        ruleset["rules"].append({
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [copy.deepcopy(check)]},
        })
    assert module["violations"](ruleset)
    candidate = tmp_path / "ruleset.json"
    candidate.write_text(json.dumps(ruleset))
    invariant = module["test_the_ruleset_definition_holds"]
    monkeypatch.setitem(invariant.__globals__, "RULESET", candidate)
    with pytest.raises(AssertionError):
        invariant()
