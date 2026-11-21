"""Probes an earlier engine committed leave the branch before it is verified again: no examiner
probe reaches main, and an open finding one witnessed keeps the probe's command and text."""

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import helpers  # noqa: F401 - puts the engine on sys.path
from implement_loop import engine as engine_module
from implement_loop.engine import Engine
from implement_loop.model import IssueState
from implement_loop.state import RunStore


class CommittedProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="implement-loop-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.engine = Engine.__new__(Engine)
        self.engine.store = RunStore(self.tmp / "run")

    def test_probes_an_earlier_engine_committed_leave_the_branch(self):
        wt, probe = self.tmp / "wt", "checks/probe_8.sh"
        (wt / "checks").mkdir(parents=True)
        (wt / probe).write_text("#!/usr/bin/env bash\ntest -f edge_8.txt\n")
        builder = {"criterion": "D1", "command": "bash checks/issue_8.sh", "cwd": ".", "author": "builder"}
        self.engine.store.put(IssueState(
            number=8, probes=[probe],
            commands=[builder, {"criterion": "D1", "command": f"bash {probe}", "cwd": ".", "author": "examiner"}],
            findings=[{"id": "E8-1-1", "status": "open", "probe": probe}]))
        commits = []
        self.engine.ws = SimpleNamespace(commit_all=lambda *a, **k: commits.append(k) or "sha")  # ty: ignore[invalid-assignment]  # test double
        self.assertTrue(self.engine._drop_probes(8, wt))
        st = self.engine.store.issue(8)
        self.assertFalse((wt / probe).exists())
        self.assertEqual(commits, [{"unlocks": [probe]}], "released in CI's loop guard as it leaves")
        self.assertEqual((st.probes, st.commands), ([], [builder]))
        self.assertEqual((st.findings[0]["reproduction"], st.findings[0][engine_module.PROBE_TEXT]),
                         (f"bash {probe}", "#!/usr/bin/env bash\ntest -f edge_8.txt\n"), "its finding still runs it")
        self.assertFalse(self.engine._drop_probes(8, wt), "once")


if __name__ == "__main__":
    unittest.main()
