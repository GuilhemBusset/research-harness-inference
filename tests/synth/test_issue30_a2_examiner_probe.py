"""Independent checks for issue 30's linting gate."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("issue30_lint", REPO / "tests/synth/test_cfn_lint.py")
assert SPEC is not None and SPEC.loader is not None
SUBJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBJECT)


def test_named_negative_control_observes_a_real_linter_error(monkeypatch):
    results = []
    original = SUBJECT.synthesized_findings

    def recording_findings(template):
        found = original(template)
        results.append((copy.deepcopy(template), found))
        return found

    monkeypatch.setattr(SUBJECT, "synthesized_findings", recording_findings)
    SUBJECT.test_reports_an_error()
    assert len(results) == 2
    assert results[0][1] == []
    errors = [m for m in results[1][1] if m.rule.severity == "error"]
    assert errors
    assert results[1][0]["Resources"]["Queue"]["Type"] != results[0][0]["Resources"]["Queue"]["Type"]
    assert any(list(m.path)[:2] == ["Resources", "Queue"] for m in errors)
