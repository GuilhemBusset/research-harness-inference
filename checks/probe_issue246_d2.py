"""The build waits without altering a worktree held by another run, then resumes."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))

from helpers import SHIP, git
from test_deploying import L, Loop


def test_engine_preserves_another_run_and_resumes_after_release():
    case = Loop(contexts=())
    try:
        case.add_work(2)
        other = L.workspace.Workspace(case.repo.work, 99, SHIP, base_dir=case.repo.tmp / "other-run")
        held = other.issue_worktree(2, "feat/issue-2")
        (held / "staged.txt").write_text("staged work\n")
        git("add", "staged.txt", cwd=held)
        (held / "untracked.txt").write_text("untracked work\n")
        before_head = other.head(held)
        before_index = git("write-tree", cwd=held)
        case.run()
        state = case.issue(2)
        assert state.phase == L.model.Phase.WAITING
        assert state.wait["kind"] == L.model.WAIT_CLAIM
        assert state.wait["resume"] == "build"
        assert not [e for e in case.stores[1].events() if e["kind"] == "step_crashed"]
        assert not [call for call in case.agents.calls if call[0] == "build"]
        assert other.head(held) == before_head
        assert git("write-tree", cwd=held) == before_index
        assert (held / "staged.txt").read_text() == "staged work\n"
        assert (held / "untracked.txt").read_text() == "untracked work\n"
        other.remove(held)
        case.run()
        assert case.issue(2).phase == L.model.Phase.DONE
        assert case.repo.main_file("docs/2.txt") == "text"
    finally:
        case.repo.cleanup()
