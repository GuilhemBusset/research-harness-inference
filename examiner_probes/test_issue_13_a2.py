import runpy

import pytest


@pytest.mark.parametrize("check", [
    {"context": "required"},
    {"context": "required", "integration_id": 42},
])
def test_constructed_unsafe_sources_are_rejected(check):
    module = runpy.run_path("tests/policy/test_ruleset.py")
    ruleset = {
        "enforcement": "active",
        "bypass_actors": [],
        "rules": [
            {"type": "pull_request"},
            {"type": "non_fast_forward"},
            {"type": "deletion"},
            {"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "required", "integration_id": 15368}],
            }},
        ],
    }
    assert module["violations"](ruleset) == []
    ruleset["rules"][-1]["parameters"]["required_status_checks"] = [check]
    assert module["violations"](ruleset)
    module["test_detects_a_check_anyone_can_set"]()
