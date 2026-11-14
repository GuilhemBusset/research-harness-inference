import os
import sys
import threading
from pathlib import Path
from unittest import mock

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


def test_concurrent_run_cannot_remove_another_scanned_push_guard(case):
    """Two runs share cli.runs_dir(root)/no-hooks, but own separate Engine locks."""
    repo = case.repo
    engines, trees, heads = [], [], []
    for number in (41, 42):
        ws = h.L.workspace.Workspace(repo.work, number, h.SHIP, base_dir=repo.tmp / f"run-{number}")
        wt = ws.issue_worktree(number, f"feat/issue-{number}")
        (wt / f"change-{number}.txt").write_text("a branch change\n")
        h.git("add", "-A", cwd=wt)
        h.git("commit", "-qm", "branch work", cwd=wt)
        engine = object.__new__(h.L.engine.Engine)
        engine.ws = ws
        engine.caps = h.L.config.Caps(check_timeout_s=10)
        engine._push_lock = threading.Lock()
        engines.append(engine)
        trees.append(wt)
        heads.append(ws.head(wt))
        assert engine._local_scan(wt, h.git("rev-parse", "origin/main", cwd=wt), heads[-1]) == ([], [])

    seed = repo.tmp / "seed"
    (seed / "new-main.txt").write_text("main changed after the scans\n")
    h.git("add", "-A", cwd=seed)
    h.git("commit", "-qm", "advance main", cwd=seed)
    h.git("push", "-q", "origin", "HEAD:main", cwd=seed)

    first_installed, second_installed, first_done, second_started = (threading.Event() for _ in range(4))
    originals = [engine.ws.push for engine in engines]
    outcomes = {}

    def first_push(wt):
        first_installed.set()
        assert second_started.wait(10)
        # A corrected implementation may serialize before installing the second
        # hook. Let the first push proceed in that case as well.
        second_installed.wait(1)
        # Both branches need rebasing, so a correct guard must withhold either new head.
        return originals[0](wt)

    def second_push(wt):
        second_installed.set()
        assert first_done.wait(10)
        return originals[1](wt)

    def run(index):
        try:
            if index == 1:
                second_started.set()
            outcomes[index] = engines[index]._push(trees[index], heads[index])
        except (h.L.workspace.GitError, AssertionError, OSError) as error:
            outcomes[index] = type(error).__name__
        finally:
            if index == 0:
                first_done.set()

    quick = repo.tmp / "quick-bin"
    quick.mkdir()
    (quick / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (quick / "sleep").chmod(0o755)
    with mock.patch.object(engines[0].ws, "push", first_push), \
            mock.patch.object(engines[1].ws, "push", second_push), \
            mock.patch.dict(os.environ, {"PATH": f"{quick}{os.pathsep}{os.environ['PATH']}"}):
        first = threading.Thread(target=run, args=(0,))
        second = threading.Thread(target=run, args=(1,))
        first.start()
        assert first_installed.wait(10)
        second.start()
        first.join(20)
        second.join(20)
        assert not first.is_alive() and not second.is_alive()
    remote = h.git("ls-remote", "--heads", "origin", "feat/issue-42", cwd=repo.work)
    assert not remote, f"a head never scanned was published; push outcomes: {outcomes}"
