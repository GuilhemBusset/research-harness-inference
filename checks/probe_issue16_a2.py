import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))
import test_history_checks as h


@pytest.fixture
def case():
    value = h.OwnersListCase(methodName="runTest")
    value.setUp()
    try:
        yield value
    finally:
        value.doCleanups()
        value.tearDown()


def test_only_the_rule_and_path_leave_the_scan(case):
    case.work()
    agents = h.OwnersValueAgents(case.scripts, rounds=1)
    case.run_engine(agents)
    assert case.ended() == ("failed", "history")
    state = case.store.issue(2)
    refusal = case.events("history_refused", 2)[0]
    assert refusal["findings"] == [h.NOTED]
    assert refusal["output"] == []
    fault = next(i for i in case.gh.issues.values() if {"name": "loop:engine-bug"} in i["labels"])
    for text in (state.fail["detail"], state.feedback[0], fault["body"]):
        assert h.NOTED in text
    for secret in h.SECRET:
        assert secret not in case.kept()
