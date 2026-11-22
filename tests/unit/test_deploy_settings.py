"""The implement loop's deployment settings agree with the pipeline's reporter (ADR 0001, decisions 5 and 6).

The table `[deploy]` of `tools/implementation-loop/loop.toml` names the commit statuses the engine
waits for after a merge, and two descriptions it compares by value: the one of an approval that
waits for the owner, and the one of a verification whose tests ran and passed, which discharges the
items that name a smoke test. The reporter posts those statuses (`rhi_reporter.status`). The
engine is vendored and imports nothing of the reporter, and the owner fills the table by hand, so
this test is what holds the two together: a context the reporter cannot post would be waited for
without end, and a description that is another member of the reporter's vocabulary (a verification
that ran no test) would discharge items nothing proved.

The verification does not run every smoke test: its script deselects some
(`scripts/pipeline/verify.py`). The table names, in `unverified_tests`, the test files of which a
test is left out, and the engine leaves an item that names one of them to the owner. This test
holds that list equal to the files of the tests the verification's own run deselects, so a test
that is newly left out cannot go on discharging the items it decides.

The engine also reads, in `discharge_contexts`, verification contexts it never waits for: only to
discharge such an item after the fact, from a later verification of the merge commit or of a `main`
that holds it. Each must be the context of a stage's verification as the reporter posts it, or the
engine would read, run after run, a status nothing posts.
"""

from __future__ import annotations

import functools
import json
import os
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import verify
from rhi_reporter import status

LOOP_TOML = Path(__file__).resolve().parents[2] / "tools" / "implementation-loop" / "loop.toml"
# Each description of the table, with the reporter's constant it must equal once it is set.
DESCRIPTIONS = {
    "verified_description": status.VERIFIED_DESCRIPTION,
    "approval_description": status.APPROVAL_DESCRIPTION,
}
A_COMMIT = "0" * 40

# A plugin of the collection below: it writes, as JSON, the files of the tests that were deselected
# and the number of tests that were kept, to the file its variable names.
RECORDER = """
import json, os

deselected = []


def pytest_deselected(items):
    deselected.extend(item.nodeid.partition("::")[0] for item in items)


def pytest_collection_finish(session):
    with open(os.environ["RECORDED"], "w", encoding="utf-8") as file:
        json.dump({"deselected": sorted(set(deselected)), "kept": len(session.items)}, file)
"""


@functools.cache
def left_out_by_the_verification() -> tuple[str, ...]:
    """The smoke test files of which the verification leaves a test out, from the repository's
    root: what the script's own run of the smoke tests deselects (`verify.run_tests`), with an
    endpoint given, so that the deployed-only tests are collected. Collection only: nothing is
    run, so nothing is reached."""
    with tempfile.TemporaryDirectory() as folder:
        recorded = Path(folder) / "recorded.json"
        (Path(folder) / "recorder.py").write_text(RECORDER, encoding="utf-8")
        environ = {n: value for n, value in os.environ.items() if not n.startswith(("RHI_SMOKE_", "PYTEST_"))}
        environ |= {
            verify.ENDPOINT_VARIABLE: "https://smoke.example.com",
            "PYTHONPATH": folder,
            "PYTEST_ADDOPTS": "-p recorder --collect-only -q -p no:cacheprovider",
            "RECORDED": str(recorded),
        }
        assert verify.run_tests(environ) == 0, "the smoke tests are not collected as the verification collects them"
        found = json.loads(recorded.read_text(encoding="utf-8"))
    assert found["kept"], "the verification would run no test"
    return tuple(found["deselected"])


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


def verification_posted_by_the_reporter(context: object) -> bool:
    """Whether `context` is `verify/<stage>` for a stage the reporter accepts: the context of the
    stage's verification, and not of its own status."""
    if not isinstance(context, str) or not posted_by_the_reporter(context):
        return False
    verification = status.status_for(
        {"stage": context.partition("/")[2], "action": status.VERIFY_ACTION, "state": "SUCCEEDED", "commit": A_COMMIT}
    )
    return context == verification.context


def problems(table: Mapping[str, object], left_out: Sequence[str] | None = None) -> list[str]:
    """What is wrong with a table `[deploy]`: nothing for the committed one, and for the owner's.
    `left_out` is the test files of which the verification leaves a test out: the real ones, unless
    a test gives its own."""
    found = []
    contexts = table.get("contexts", [])
    for context in contexts if isinstance(contexts, list) else [contexts]:
        if not posted_by_the_reporter(context):
            found.append(f"the context {context!r} is neither deploy/<stage> nor verify/<stage>")
    discharge = table.get("discharge_contexts", [])
    for context in discharge if isinstance(discharge, list) else [discharge]:
        if not verification_posted_by_the_reporter(context):
            found.append(f"the discharge context {context!r} is not a verify/<stage> context the reporter posts")
    for key, constant in DESCRIPTIONS.items():
        value = table.get(key, "")
        if value != "" and not (isinstance(value, str) and value == str(constant)):
            found.append(f"{key} is set, and it is not the reporter's {constant.name}")
    if "unverified_tests" in table or table.get("verified_description", "") != "":
        left_out = sorted(left_out_by_the_verification() if left_out is None else left_out)
        named = table.get("unverified_tests")
        if not (isinstance(named, list) and sorted(named, key=str) == left_out):
            found.append(f"unverified_tests is not the test files of which the verification leaves a test out: {left_out}")
    return found


def owners_table(**changes: object) -> dict[str, object]:
    """The table the owner is expected to write, with one value changed."""
    table = {
        "contexts": ["deploy/beta", "verify/beta"],
        "discharge_contexts": ["verify/beta"],
        "timeout_s": 1800,
        "approval_description": str(status.APPROVAL_DESCRIPTION),
        "verified_description": str(status.VERIFIED_DESCRIPTION),
        "unverified_tests": list(left_out_by_the_verification()),
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


@pytest.mark.parametrize("context", ["deploy/beta", "verify/", "Verify/beta", "verify/a stage", "verify/beta/x", "beta", 1])
def test_a_discharge_context_that_is_no_verification_the_reporter_posts_fails(context: object) -> None:
    [problem] = problems(owners_table(discharge_contexts=["verify/beta", context]))
    assert problem.startswith("the discharge context") and repr(context) in problem


@pytest.mark.parametrize("key", list(DESCRIPTIONS))
def test_a_description_that_is_another_member_of_the_vocabulary_fails(key: str) -> None:
    others = [member for member in status.Description if member is not DESCRIPTIONS[key]]
    assert status.Description.NOT_RUN_CANARY_MISSING in others, "such as a verification that ran no test"
    for member in others:
        [problem] = problems(owners_table(**{key: str(member)}))
        assert problem.startswith(key), member.name
    # and so does the constant with a typo in it
    assert problems(owners_table(**{key: str(DESCRIPTIONS[key]).rstrip(".")}))


# A file the verification runs, and the file that holds the GPU-only smoke test, which its own run
# on the mock pool leaves out.
RUN, LEFT_OUT = "tests/smoke/test_smoke.py", "tests/smoke/deployed/test_gpu_release.py"


@pytest.mark.parametrize(
    "named",
    [[], [RUN], [RUN, LEFT_OUT], LEFT_OUT, None],
    ids=["none", "another", "one-more", "not-a-list", "not-given"],
)
def test_a_table_that_does_not_name_the_tests_the_verification_leaves_out_fails(named: object) -> None:
    assert problems(owners_table(unverified_tests=[LEFT_OUT]), left_out=[LEFT_OUT]) == []
    table = owners_table(unverified_tests=named)
    if named is None:
        del table["unverified_tests"]   # with a verified_description, the engine would discharge nothing
    [problem] = problems(table, left_out=[LEFT_OUT])
    assert problem.startswith("unverified_tests") and LEFT_OUT in problem, "it says what to write"
    # A list is held to the verification even before a description is set.
    if named is not None:
        assert problems({"unverified_tests": named}, left_out=[LEFT_OUT]) == [problem]


def test_the_files_left_out_are_read_from_the_verification_s_own_run() -> None:
    files = left_out_by_the_verification()
    assert sorted(files) == [LEFT_OUT], "the run on the mock pool leaves out the GPU-only test's file"
    assert all(file.startswith("tests/smoke/") and (verify.REPO / file).is_file() for file in files)
