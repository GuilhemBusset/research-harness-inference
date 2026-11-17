"""Which of the examiner's findings count as naming a seeded defect: those that state the defect,
not those that only touch the same file or reuse the words around it."""

from __future__ import annotations

import pytest

import run
from implement_loop.agents import ExamResult
from implement_loop.model import Finding

MANIFEST = run.load_manifest()

# Findings on something else in the patched file, in the vocabulary of the code around the defects.
UNRELATED = (
    ("Extract a constant for the artifact bucket path", "The bucket path expression is duplicated.",
     "Use a shared constant for maintainability."),
    ("Extract a constant for the wildcard marker", "The string literal is repeated.",
     "Use a named constant for maintainability."),
    ("Name the internet-facing load balancer's port", "The prefix and the ipv6 comment are written twice.",
     "Write the invariants' names in one place and retain one comment."),
)


def caught(defect: run.Defect, finding: Finding) -> bool:
    result = ExamResult(verdicts=[], findings=[finding], probes=[], decisions=[], certify=False)
    return run.score(defect, result, MANIFEST.classes[defect.cls]).caught


@pytest.mark.parametrize("defect", MANIFEST.defects, ids=lambda defect: defect.id)
def test_a_finding_that_states_the_defect_counts(defect: run.Defect) -> None:
    assert caught(defect, Finding(id="", title="Broader than the task", file=defect.file, observed=defect.description))


@pytest.mark.parametrize("defect", MANIFEST.defects, ids=lambda defect: defect.id)
def test_a_finding_on_something_else_in_the_file_stays_a_miss(defect: run.Defect) -> None:
    for title, observed, expected in UNRELATED:
        finding = Finding(id="", title=title, file=defect.file, observed=observed, expected=expected)
        assert not caught(defect, finding), title
    reworded = Finding(id="", title=f"Reword the docstring: {defect.task}", file=defect.file, quote=defect.task)
    assert not caught(defect, reworded)
