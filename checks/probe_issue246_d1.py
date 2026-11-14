"""Exercise the git refusal and recovery with committed file contents."""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/implementation-loop/tests"))

from helpers import SHIP, TempRepo, git
from test_deploying import L


def test_stale_registration_recovers_committed_work():
    repo = TempRepo()
    try:
        ws = L.workspace.Workspace(repo.work, 1, SHIP, base_dir=repo.tmp / "recovery")
        branch = "feat/preserved-work"
        original = ws.issue_worktree(2, branch)
        (original / "preserved.txt").write_text("committed before deletion\n")
        git("add", "preserved.txt", cwd=original)
        git("commit", "-qm", "preserve work", cwd=original)
        before = ws.head(original)
        shutil.rmtree(original)
        with pytest.raises(L.workspace.GitError):
            ws.git("worktree", "add", "--quiet", str(original), branch)
        recovered = ws.issue_worktree(2, branch)
        assert recovered == original
        assert ws.head(recovered) == before
        assert (recovered / "preserved.txt").read_text() == "committed before deletion\n"
        assert git("symbolic-ref", "--short", "HEAD", cwd=recovered) == branch
    finally:
        repo.cleanup()
