import runpy


def test_constructed_bypass_is_rejected():
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
    ruleset["bypass_actors"] = [
        {"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"}
    ]
    assert module["violations"](ruleset)
    module["test_detects_a_bypass"]()
