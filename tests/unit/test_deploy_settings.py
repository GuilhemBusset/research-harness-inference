"""The implement loop's deployment settings agree with the pipeline's reporter (ADR 0001, decisions 5 and 6).

The table `[deploy]` of `tools/implementation-loop/loop.toml` names the commit statuses the engine
waits for after a merge, and two descriptions it compares by value: the one of an approval that
waits for the owner, and the one of a verification whose tests ran and passed, which discharges the
items that name a smoke test. The reporter posts those statuses (`rhi_reporter.status`). The
engine is vendored and imports nothing of the reporter, and the owner fills the table by hand, so
this test is what holds the two together: a context the reporter cannot post would be waited for
without end, and a description that is another member of the reporter's vocabulary (a verification
that ran no test) would discharge items nothing proved.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest

from rhi_reporter import status

LOOP_TOML = Path(__file__).resolve().parents[2] / "tools" / "implementation-loop" / "loop.toml"
# Each description of the table, with the reporter's constant it must equal once it is set.
DESCRIPTIONS = {
    "verified_description": status.VERIFIED_DESCRIPTION,
    "approval_description": status.APPROVAL_DESCRIPTION,
}
A_COMMIT = "0" * 40


def posted_by_the_reporter(context: object) -> bool:
    """Whether `context` is `deploy/<stage>` or `verify/<stage>` for a stage the reporter accepts:
    the context of the stage's own status, or of its verification's."""
    if not isinstance(context, str):
        return False
    stage = context.partition("/")[2]
    try:
        own = status.status_for({"stage": stage, "state": "SUCCEEDED", "commit": A_COMMIT})
        verification = status.status_for({"stage": stage, "action": status.VERIFY_ACTION, "state": "SUCCEEDED", "commit": A_COMMIT})
    except ValueError:
        return False
    return context in (own.context, verification.context)


def problems(table: Mapping[str, object]) -> list[str]:
    """What is wrong with a table `[deploy]`: nothing for the committed one, and for the owner's."""
    found = []
    contexts = table.get("contexts", [])
    for context in contexts if isinstance(contexts, list) else [contexts]:
        if not posted_by_the_reporter(context):
            found.append(f"the context {context!r} is neither deploy/<stage> nor verify/<stage>")
    for key, constant in DESCRIPTIONS.items():
        value = table.get(key, "")
        if value != "" and not (isinstance(value, str) and value == str(constant)):
            found.append(f"{key} is set, and it is not the reporter's {constant.name}")
    return found


def owners_table(**changes: object) -> dict[str, object]:
    """The table the owner is expected to write, with one value changed."""
    table = {
        "contexts": ["deploy/beta", "verify/beta"],
        "timeout_s": 1800,
        "approval_description": str(status.APPROVAL_DESCRIPTION),
        "verified_description": str(status.VERIFIED_DESCRIPTION),
    }
    return table | changes


def test_the_committed_table_agrees_with_the_reporter() -> None:
    assert problems(tomllib.loads(LOOP_TOML.read_text(encoding="utf-8"))["deploy"]) == []


def test_the_table_the_owner_is_expected_to_write_passes() -> None:
    assert problems(owners_table()) == []
    assert problems(owners_table(approval_description="", verified_description="")) == [], "an empty description is unset"


@pytest.mark.parametrize(
    "context",
    ["measure/beta", "deploy", "verify/", "deploy/beta/tests", "Verify/beta", "verify/a stage", " deploy/beta", "beta"],
)
def test_a_context_of_another_form_fails(context: str) -> None:
    [problem] = problems(owners_table(contexts=["deploy/beta", context]))
    assert repr(context) in problem


@pytest.mark.parametrize("key", list(DESCRIPTIONS))
def test_a_description_that_is_another_member_of_the_vocabulary_fails(key: str) -> None:
    others = [member for member in status.Description if member is not DESCRIPTIONS[key]]
    assert status.Description.NOT_RUN_CANARY_MISSING in others, "such as a verification that ran no test"
    for member in others:
        [problem] = problems(owners_table(**{key: str(member)}))
        assert problem.startswith(key), member.name
    # and so does the constant with a typo in it
    assert problems(owners_table(**{key: str(DESCRIPTIONS[key]).rstrip(".")}))
