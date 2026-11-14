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


@pytest.mark.parametrize("earlier", [False, True])
def test_private_tree_and_history_are_refused_and_retry_is_fresh(case, earlier):
    case.work(wrong_attempts=int(earlier))
    agents = h.OwnersValueAgents(case.scripts, rounds=1)
    case.run_engine(agents)
    state = case.store.issue(2)
    assert case.ended() == ("failed", "history")
    assert not case.gh.prs
    assert "feat/issue-2" not in case.remote_branches()
    refusal = case.events("history_refused", 2)[0]
    assert refusal["findings"] == [h.NOTED]
    assert refusal["output"] == []
    for secret in h.SECRET:
        assert secret not in case.kept()
    assert h.NOTED in state.fail["detail"]
    assert h.NOTED in state.feedback[0]
    assert (h.NOTE in h.git("ls-tree", "-r", "--name-only", refusal["head"], cwd=case.repo.work)) != earlier
    assert state.branch == "feat/issue-2-r2"
    case.retry()
    assert case.run_engine(agents)["phases"] == {"done": 1}
    assert "feat/issue-2-r2" in case.remote_branches()
    assert h.VALUE not in case.published()


def test_revert_is_scanned_and_withheld(case):
    case.values.write_text("This reverts commit\n")
    case.issue(2, "alpha")
    case.issue(3, "broken", blocked_by=(2,))
    case.epic(2, 3)
    case.run_engine()
    assert case.ended(3) == ("failed", "main_broken")
    assert "<commit>: local-value" in case.store.issue(3).fail["detail"]
    assert "-revert" not in case.remote_branches()
