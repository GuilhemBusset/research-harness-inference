"""The corpus of seeded defects, and the catch rate the runner computes from an examiner's answers.

Nothing here applies a patch: the patches are written against the commit the manifest records, and
the tree they would be applied to here has moved on since.
"""

from __future__ import annotations

import re
import tomllib
from collections import Counter

import pytest

import run
from implement_loop.agents import ExamResult
from implement_loop.model import Finding

MANIFEST = run.load_manifest()
LOOP_TOML = run.ENGINE / "loop.toml"
HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def files_and_hunks(text: str) -> tuple[list[str], list[str], int]:
    """The `---` and `+++` names of a unified diff and its number of hunks, each hunk read to the
    line counts its header gives. Anything after a hunk that is not another hunk fails."""
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("@@"))
    old = [line[4:] for line in lines[:first] if line.startswith("--- ")]
    new = [line[4:] for line in lines[:first] if line.startswith("+++ ")]
    hunks, i = 0, first
    while i < len(lines):
        header = HUNK.match(lines[i])
        assert header, f"not a hunk header: {lines[i]!r}"
        old_left, new_left = int(header[1] or 1), int(header[2] or 1)
        hunks, i = hunks + 1, i + 1
        while old_left or new_left:
            tag = lines[i][:1]
            i += 1
            assert tag in (" ", "-", "+", "\\"), f"not a line of a hunk: {lines[i - 1]!r}"
            old_left -= tag in (" ", "-")
            new_left -= tag in (" ", "+")
            assert old_left >= 0 and new_left >= 0, "a hunk longer than its header says"
        while i < len(lines) and lines[i].startswith("\\"):
            i += 1
    return old, new, hunks


def test_the_manifest_names_twelve_defects_or_more_three_or_more_in_each_of_four_classes() -> None:
    assert len(MANIFEST.defects) >= 12
    per_class = Counter(defect.cls for defect in MANIFEST.defects)
    assert len(MANIFEST.classes) == 4
    assert set(per_class) == set(MANIFEST.classes)
    assert min(per_class.values()) >= 3


def test_the_manifest_records_a_commit_and_what_each_defect_is() -> None:
    data = tomllib.loads(run.MANIFEST.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", MANIFEST.commit)
    for entry in data["defect"]:
        assert entry["class"] in MANIFEST.classes
        assert entry["file"] and entry["description"].endswith(".") and entry["task"]
        assert isinstance(entry["caught_by_tests"], bool)
    assert len({defect.id for defect in MANIFEST.defects}) == len(MANIFEST.defects)


def test_each_class_names_safety_invariants_the_examiner_already_knows() -> None:
    known = set(tomllib.loads(LOOP_TOML.read_text(encoding="utf-8"))["invariants"]["names"])
    for invariants in MANIFEST.classes.values():
        assert invariants and set(invariants) <= known


def test_the_folder_holds_a_patch_for_each_defect_and_no_other() -> None:
    folder = run.MANIFEST.parent
    assert {p.name for p in folder.glob("*.patch")} == {f"{defect.id}.patch" for defect in MANIFEST.defects}


@pytest.mark.parametrize("defect", MANIFEST.defects, ids=lambda defect: defect.id)
def test_each_patch_is_a_unified_diff_of_its_entrys_file_only(defect: run.Defect) -> None:
    old, new, hunks = files_and_hunks(defect.patch.read_text(encoding="utf-8"))
    assert old == [f"a/{defect.file}"]
    assert new == [f"b/{defect.file}"]
    assert hunks >= 1


def answer(defect: run.Defect, how: str) -> ExamResult:
    """A fake examiner's answer on `defect`, of one of five kinds."""
    invariant = MANIFEST.classes[defect.cls][0]
    findings = {
        "the invariant": [Finding(id="", title="Broader than asked", file=defect.file, invariant=invariant)],
        "its words": [Finding(id="", title=f"{defect.file} adds {defect.match[0]}", item="D1")],
        "another file": [Finding(id="", title="Broader than asked", file="src/rhi/stage.py", invariant=invariant)],
        "something else": [Finding(id="", title="Naming could be clearer", file=defect.file)],
        "nothing": [],
    }[how]
    return ExamResult(verdicts=[], findings=findings, probes=[], decisions=[], certify=not findings)


def test_the_catch_rate_per_class_counts_the_findings_that_name_the_defect() -> None:
    kinds = ["the invariant", "its words", "another file", "something else", "nothing"]
    outcomes, expected = [], {}
    for name in MANIFEST.classes:
        defects = [defect for defect in MANIFEST.defects if defect.cls == name]
        for index, defect in enumerate(defects):
            outcomes.append(run.score(defect, answer(defect, kinds[index % len(kinds)]), MANIFEST.classes[name]))
        caught = sum(kinds[index % len(kinds)] in ("the invariant", "its words") for index in range(len(defects)))
        expected[name] = {"examined": len(defects), "caught": caught, "failed": 0, "rate": caught / len(defects)}
    outcomes.append(run.Outcome("broken", "gate-weakened", caught=False, certified=False, caught_by_tests=False,
                                error="codex examine failed"))
    expected["gate-weakened"]["failed"] = 1

    assert run.catch_rates(outcomes, list(MANIFEST.classes)) == expected

