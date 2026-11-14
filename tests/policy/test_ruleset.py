"""The ruleset on `main` keeps its guarantees (system design, section 8.4; evidence E26).

GitHub lets anyone with write access set a commit status of any name, unless the ruleset names the
source it expects for each required check. The coding agents write commit statuses, so a ruleset
that names no source would let an agent mark its own head green. This invariant reads the ruleset
definition, `setup/pr-only-main.json`, not a synthesized template.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

RULESET = Path(__file__).resolve().parents[2] / "setup" / "pr-only-main.json"

# The application ID of GitHub Actions, the same on every repository of github.com.
GITHUB_ACTIONS = 15368
REQUIRED_CHECK = "required"
# The pull-request rule, the rule against force-pushes and the rule against deletion.
REQUIRED_RULES = ("pull_request", "non_fast_forward", "deletion")


def violations(ruleset: dict[str, Any]) -> list[str]:
    """What the ruleset lacks to be the fourth control of the trust boundary; empty when it holds."""
    found = []
    if ruleset.get("enforcement") != "active":
        found.append("enforcement is not active")
    if ruleset.get("bypass_actors") != []:
        found.append("the bypass list is not empty")
    rules = ruleset.get("rules", [])
    types = {rule.get("type") for rule in rules}
    found.extend(f"the {name} rule is missing" for name in REQUIRED_RULES if name not in types)
    checks = [
        check
        for rule in rules
        if rule.get("type") == "required_status_checks"
        for check in rule.get("parameters", {}).get("required_status_checks", [])
    ]
    if REQUIRED_CHECK not in {check.get("context") for check in checks}:
        found.append(f"the check {REQUIRED_CHECK} is not required")
    found.extend(
        f"anyone can set the check {check.get('context')}"
        for check in checks
        if check.get("integration_id") != GITHUB_ACTIONS
    )
    return found


def compliant() -> dict[str, Any]:
    """A ruleset built here, not read from `setup/`, that holds every guarantee."""
    return {
        "enforcement": "active",
        "rules": [
            {"type": "pull_request", "parameters": {"required_approving_review_count": 0}},
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": REQUIRED_CHECK, "integration_id": GITHUB_ACTIONS}]
                },
            },
            {"type": "non_fast_forward"},
            {"type": "deletion"},
        ],
        "bypass_actors": [],
    }


def with_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    ruleset = compliant()
    ruleset["rules"][1]["parameters"]["required_status_checks"] = copy.deepcopy(checks)
    return ruleset


def test_the_ruleset_definition_holds():
    assert violations(json.loads(RULESET.read_text(encoding="utf-8"))) == []


def test_a_compliant_ruleset_has_no_violation():
    assert violations(compliant()) == []


def test_detects_a_check_anyone_can_set():
    expected = ["anyone can set the check required"]
    assert violations(with_checks([{"context": REQUIRED_CHECK}])) == expected
    assert violations(with_checks([{"context": REQUIRED_CHECK, "integration_id": None}])) == expected
    assert violations(with_checks([{"context": REQUIRED_CHECK, "integration_id": GITHUB_ACTIONS + 1}])) == expected
    # Every required check names its source, not only the one named required.
    both = [{"context": REQUIRED_CHECK, "integration_id": GITHUB_ACTIONS}, {"context": "other"}]
    assert violations(with_checks(both)) == ["anyone can set the check other"]


def test_detects_a_bypass():
    ruleset = compliant()
    ruleset["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
    assert violations(ruleset) == ["the bypass list is not empty"]


def test_detects_that_the_required_check_is_not_required():
    expected = ["the check required is not required"]
    assert violations(with_checks([])) == expected
    assert violations(with_checks([{"context": "other", "integration_id": GITHUB_ACTIONS}])) == expected


def test_detects_an_enforcement_that_is_not_active():
    for enforcement in ("disabled", "evaluate"):
        ruleset = compliant()
        ruleset["enforcement"] = enforcement
        assert violations(ruleset) == ["enforcement is not active"]


@pytest.mark.parametrize("missing", REQUIRED_RULES)
def test_detects_a_missing_rule(missing: str):
    ruleset = compliant()
    ruleset["rules"] = [rule for rule in ruleset["rules"] if rule["type"] != missing]
    assert violations(ruleset) == [f"the {missing} rule is missing"]
