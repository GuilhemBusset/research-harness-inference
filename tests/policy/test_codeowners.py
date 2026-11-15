"""The owner reviews a change to the trust boundary, and nothing else (system design, section 8.4, control 5).

`.github/CODEOWNERS` names one owner for exactly the paths the engine protects, `PROTECTED_PREFIXES`
and `PROTECTED_FILES` of its configuration module, and the ruleset on `main` requires a code owner's
review. A protected path without an entry would merge with no person looking; an entry on any other
path would make ordinary backlog work wait for the owner (ADR 0001, amendment 3).
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
RULESET = ROOT / "setup" / "pr-only-main.json"
ENGINE_CONFIG = ROOT / "tools" / "implementation-loop" / "implement_loop" / "config.py"

# A user's login. A team (`@org/team`) or an e-mail address is not one person the ruleset waits for.
LOGIN = re.compile(r"@[A-Za-z0-9-]+")
OWNER = "@owner"


def protected_paths() -> tuple[str, ...]:
    """The engine's own lists, read from its module, which is not on the tests' import path."""
    spec = importlib.util.spec_from_file_location("implement_loop_config", ENGINE_CONFIG)
    assert spec is not None
    assert spec.loader is not None
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    return (*config.PROTECTED_PREFIXES, *sorted(config.PROTECTED_FILES))


PROTECTED = protected_paths()


def entries(text: str) -> list[tuple[str, list[str]]]:
    """The (pattern, owners) of each line of a CODEOWNERS file that is not blank or a comment."""
    lines = (line.split("#", 1)[0].split() for line in text.splitlines())
    return [(fields[0], fields[1:]) for fields in lines if fields]


def codeowners_violations(text: str, protected: Iterable[str]) -> list[str]:
    """How a CODEOWNERS file departs from one owner on exactly the protected paths; empty when it holds."""
    found = []
    listed = entries(text)
    patterns = [pattern for pattern, _ in listed]
    # A protected path is relative to the root of the repository; its entry is anchored there.
    expected = [f"/{path}" for path in protected]
    found.extend(f"the protected path {pattern} has no owner entry" for pattern in expected if pattern not in patterns)
    found.extend(f"the unprotected path {pattern} has an owner entry" for pattern in patterns if pattern not in expected)
    found.extend(f"{pattern} has more than one entry" for pattern in sorted(set(patterns)) if patterns.count(pattern) > 1)
    found.extend(
        f"{pattern} does not name one user as its owner"
        for pattern, owners in listed
        if len(owners) != 1 or not LOGIN.fullmatch(owners[0])
    )
    if len({tuple(owners) for _, owners in listed}) > 1:
        found.append("the entries do not all name the same owner")
    return found


def ruleset_violations(ruleset: dict[str, Any]) -> list[str]:
    """Empty when the pull-request rule of the ruleset requires a code owner's review."""
    required = [
        rule.get("parameters", {}).get("require_code_owner_review")
        for rule in ruleset.get("rules", [])
        if rule.get("type") == "pull_request"
    ]
    return [] if required and all(value is True for value in required) else ["no code owner's review is required"]


def compliant(protected: Iterable[str] = PROTECTED) -> str:
    """A CODEOWNERS text built here, not read from `.github/`, that holds."""
    return "# A comment.\n\n" + "".join(f"/{path} {OWNER}\n" for path in protected)


def test_codeowners_names_the_owner_on_exactly_the_protected_paths():
    assert PROTECTED
    assert codeowners_violations(CODEOWNERS.read_text(encoding="utf-8"), PROTECTED) == []


def test_the_ruleset_requires_a_code_owners_review():
    assert ruleset_violations(json.loads(RULESET.read_text(encoding="utf-8"))) == []


def test_a_compliant_file_has_no_violation():
    assert codeowners_violations(compliant(), PROTECTED) == []


@pytest.mark.parametrize("missing", PROTECTED)
def test_detects_a_protected_path_without_an_owner_entry(missing: str):
    text = compliant(path for path in PROTECTED if path != missing)
    assert codeowners_violations(text, PROTECTED) == [f"the protected path /{missing} has no owner entry"]
    # An entry that is commented out gives the path no owner either.
    assert codeowners_violations(compliant().replace(f"/{missing} {OWNER}\n", f"# /{missing} {OWNER}\n"), PROTECTED)


@pytest.mark.parametrize("unprotected", ["*", "/src/", "/docs/design/system-design.md", "tests/policy/"])
def test_detects_an_unprotected_path_with_an_owner_entry(unprotected: str):
    # The last one is not anchored at the root: it would match a folder of that name anywhere.
    text = compliant() + f"{unprotected} {OWNER}\n"
    assert codeowners_violations(text, PROTECTED) == [f"the unprotected path {unprotected} has an owner entry"]


@pytest.mark.parametrize("owners", ["", "@owner @other", "@org/team", "owner@example.com", "@other"])
def test_detects_an_entry_that_does_not_name_the_one_owner(owners: str):
    # Nobody, two owners, a team, an address, and a user who is not the owner of the other paths.
    text = compliant().replace(f"/{PROTECTED[0]} {OWNER}\n", f"/{PROTECTED[0]} {owners}\n")
    assert codeowners_violations(text, PROTECTED)


def test_detects_a_later_entry_that_takes_the_owner_away():
    # GitHub applies the last entry that matches a path: one without an owner removes the review.
    found = codeowners_violations(compliant() + f"/{PROTECTED[0]}\n", PROTECTED)
    assert f"/{PROTECTED[0]} has more than one entry" in found


def pull_request_rule(parameters: dict[str, Any]) -> dict[str, Any]:
    return {"rules": [{"type": "pull_request", "parameters": parameters}, {"type": "deletion"}]}


def test_a_ruleset_that_requires_the_review_has_no_violation():
    assert ruleset_violations(pull_request_rule({"require_code_owner_review": True})) == []


@pytest.mark.parametrize(
    "ruleset",
    [
        pull_request_rule({"require_code_owner_review": False}),
        pull_request_rule({"required_approving_review_count": 0}),
        {"rules": [{"type": "pull_request"}]},
        {"rules": [{"type": "deletion"}]},
        {},
    ],
)
def test_detects_a_ruleset_that_does_not_require_the_review(ruleset: dict[str, Any]):
    assert ruleset_violations(ruleset) == ["no code owner's review is required"]
