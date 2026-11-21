"""Files and tests main deletes or renames on purpose (the examiner's probes an earlier engine
committed, a test a reviewed merge replaced) are no change of a branch to a locked test."""

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401 - puts the engine on sys.path
from implement_loop import engine as engine_module
from implement_loop import verify
from implement_loop.config import Caps
from implement_loop.engine import CommittedFile, Engine, in_tree
from implement_loop.model import IssueState
from implement_loop.state import RunStore


class DeletedOnMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="implement-loop-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.engine = Engine.__new__(Engine)
        self.engine.store = RunStore(self.tmp / "run")

    def test_tests_that_ran_in_a_copy_outside_the_tree_are_never_required(self):
        # a command that copies the tree to a fresh `mktemp -d` and runs pytest there: the plugin
        # keeps those ids absolute, and the copy's path changes on every run
        manifest = {"*": ["tests/test_a.py::test_x", "/tmp/tmp.AbC123/tests/test_a.py::test_x"]}
        self.assertEqual(in_tree(manifest), {"*": ["tests/test_a.py::test_x"]})
        self.engine.store.put(IssueState(number=9, manifest=manifest))
        seen = []
        self.engine._specs = lambda st: []  # ty: ignore[invalid-assignment]  # test double
        self.engine.caps, self.engine.check_env = Caps(), {}
        self.engine._mounts = lambda root: None  # ty: ignore[invalid-assignment]  # test double
        with mock.patch.object(engine_module.verify, "verify",
                               lambda root, checks, head, work_dir, locked, tests, manifest, *a, **k: seen.append(manifest)):
            self.engine._verify_in(self.tmp, self.engine.store.issue(9), "abc123", self.tmp)
        self.assertEqual(seen, [{"*": ["tests/test_a.py::test_x"]}])

    def test_a_locked_file_main_deleted_leaves_the_lock(self):
        wt = self.tmp / "wt"
        (wt / "tests").mkdir(parents=True)
        (wt / "tests/test_kept.py").write_text("kept\n")
        before = {"tests/test_kept.py": b"kept\n", "tests/test_gone.py": b"gone\n", "tests/test_edited.py": b"edited\n"}
        locked = {f: verify.digest(CommittedFile.holding(f, b"gone\n" if f == "tests/test_edited.py" else data))
                  for f, data in before.items()}   # the branch had changed test_edited.py before main's merge
        self.engine.store.put(IssueState(number=7, locked=locked, tests=sorted(locked),
                                         manifest={"*": ["tests/test_gone.py::t", "tests/test_kept.py::t"]}))
        self.engine.ws = SimpleNamespace(show_bytes=lambda wt, ref, f: before.get(f))  # ty: ignore[invalid-assignment]  # test double
        self.engine._drop_deleted_locks(7, wt, "before")
        st = self.engine.store.issue(7)
        self.assertEqual(sorted(st.locked), ["tests/test_edited.py", "tests/test_kept.py"])
        self.assertEqual((st.tests, st.manifest), (["tests/test_edited.py", "tests/test_kept.py"],
                                                   {"*": ["tests/test_kept.py::t"]}))

    def test_probes_an_earlier_engine_committed_leave_the_branch(self):
        wt, probe = self.tmp / "wt", "checks/probe_8.sh"
        (wt / "checks").mkdir(parents=True)
        (wt / probe).write_text("#!/usr/bin/env bash\ntest -f edge_8.txt\n")
        builder = {"criterion": "D1", "command": "bash checks/issue_8.sh", "cwd": ".", "author": "builder"}
        self.engine.store.put(IssueState(
            number=8, probes=[probe], locked={probe: "d", "checks/issue_8.sh": "e"},
            manifest={"*": [f"{probe}::t", "checks/issue_8.sh::t"]},
            commands=[builder, {"criterion": "D1", "command": f"bash {probe}", "cwd": ".", "author": "examiner"}],
            findings=[{"id": "E8-1-1", "status": "open", "probe": probe}]))
        commits = []
        self.engine.ws = SimpleNamespace(commit_all=lambda *a, **k: commits.append(k) or "sha")  # ty: ignore[invalid-assignment]  # test double
        self.assertTrue(self.engine._drop_probes(8, wt))
        st = self.engine.store.issue(8)
        self.assertFalse((wt / probe).exists())
        self.assertEqual(commits, [{"unlocks": [probe]}])
        self.assertEqual((st.probes, st.commands, sorted(st.locked), st.manifest),
                         ([], [builder], ["checks/issue_8.sh"], {"*": ["checks/issue_8.sh::t"]}))
        self.assertEqual((st.findings[0]["reproduction"], st.findings[0][engine_module.PROBE_TEXT]),
                         (f"bash {probe}", "#!/usr/bin/env bash\ntest -f edge_8.txt\n"), "its finding still runs it")
        self.assertFalse(self.engine._drop_probes(8, wt), "once")


if __name__ == "__main__":
    unittest.main()
