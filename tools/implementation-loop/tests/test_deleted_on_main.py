"""Files and tests main deletes or renames on purpose (the examiner's probes an earlier engine
committed, a test a reviewed merge replaced) are no regression of the next merge, and no change
of a branch to a locked test."""

import contextlib
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
from implement_loop.engine import CommittedFile, Engine, in_tree, named_nodes, named_tests
from implement_loop.model import IssueState
from implement_loop.state import RunStore


class DeletedOnMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="implement-loop-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.engine = Engine.__new__(Engine)
        self.engine.store = RunStore(self.tmp / "run")

    def test_a_check_names_its_test_files(self):
        self.assertEqual(named_tests({"command": "uv run --locked pytest -q tests/unit/test_a.py::test_x -k b"}),
                         ["tests/unit/test_a.py"])
        self.assertEqual(named_tests({"command": "bash checks/probe_2.sh", "cwd": "sub"}), ["sub/checks/probe_2.sh"])
        self.assertEqual(named_tests({"command": "uv run --locked pytest tests/", "cwd": "."}), [])
        self.assertEqual(named_tests({"command": "uv run --no-project --python 3.12 --with pytest==8.3.5 python -m pytest -q "
                                                 "examiner_probe_costs_cli.py", "cwd": "."}),
                         ["examiner_probe_costs_cli.py"], "a probe an earlier engine committed at the root")
        self.assertEqual(named_nodes({"command": "pytest -q tests/test_a.py::Case::test_x tests/test_b.py", "cwd": "."}),
                         ["tests/test_a.py::Case::test_x"])

    def regressions(self, coord, checks, manifest):
        """The checks and manifest `_regressions` re-runs for an earlier issue #5, and those it skips."""
        self.engine.store.put_section("accepted_checks", "5", {"checks": checks, "check_files": [],
                                                               "manifest": manifest})
        ran = []

        def recorded(root, checks, head, work_dir, locked, files, manifest, *args, **kwargs):
            ran.append(([c.command for c in checks], manifest))
            return True, [], [], []
        self.engine.ws = SimpleNamespace(disposable=contextlib.nullcontext)  # ty: ignore[invalid-assignment]  # test double
        self.engine.caps, self.engine.check_env = Caps(), {}
        self.engine._mounts = lambda scratch: None  # ty: ignore[invalid-assignment]  # test double
        with mock.patch.object(engine_module.verify, "verify", recorded):
            self.assertEqual(self.engine._regressions(6, coord, "abc123"), [])
        [skipped] = [e for e in self.engine.store.events() if e["kind"] == "regression_check_skipped"]
        return ran, skipped["checks"]

    def test_regressions_skip_examiner_checks_and_checks_of_deleted_files(self):
        coord = self.tmp / "coord"
        (coord / "tests").mkdir(parents=True)
        (coord / "tests/test_kept.py").write_text("def test_a():\n    pass\n")
        ran, skipped = self.regressions(coord, [
            {"criterion": "D1", "command": "pytest tests/test_kept.py", "cwd": ".", "author": "builder"},
            {"criterion": "D2", "command": "pytest tests/test_gone.py", "cwd": ".", "author": "builder"},
            {"criterion": "A1", "command": "bash checks/probe_5.sh", "cwd": ".", "author": "examiner"}],
            {"*": ["tests/test_kept.py::test_a", "tests/test_gone.py::test_b"]})
        self.assertEqual(ran, [(["pytest tests/test_kept.py"], {"*": ["tests/test_kept.py::test_a"]})])
        self.assertEqual(skipped, ["pytest tests/test_gone.py", "bash checks/probe_5.sh"])

    def test_regressions_skip_checks_of_tests_main_removed_from_a_kept_file(self):
        coord = self.tmp / "coord"
        (coord / "tests").mkdir(parents=True)
        (coord / "tests/test_kept.py").write_text("class Case:\n    def test_a(self):\n        pass\n")
        (coord / "tests/test_other.py").write_text("def test_c():\n    pass\n")
        (coord / "tests/unit").mkdir()
        (coord / "tests/unit/test_d.py").write_text("def test_d():\n    pass\n")
        ran, skipped = self.regressions(coord, [
            {"criterion": "D1", "command": "pytest tests/test_kept.py::Case::test_a", "cwd": ".", "author": "builder"},
            {"criterion": "D2", "command": "pytest tests/test_kept.py::Case::test_renamed", "cwd": ".",
             "author": "builder"},
            {"criterion": "D3", "command": "pytest tests/test_other.py tests/test_gone.py", "cwd": ".",
             "author": "builder"},
            {"criterion": "A1", "command": "pytest tests/", "cwd": ".", "author": "builder"}],
            {"*": ["tests/test_kept.py::Case::test_a", "tests/test_kept.py::Case::test_renamed[x]",
                   "tests/test_other.py::test_c", "tests/unit/test_d.py::test_d"]})
        self.assertEqual(ran, [(["pytest tests/test_kept.py::Case::test_a", "pytest tests/"],
                                {"*": ["tests/unit/test_d.py::test_d"]})],
                         "the tests of the files the skipped checks name are no longer required")
        self.assertEqual(skipped, ["pytest tests/test_kept.py::Case::test_renamed",
                                   "pytest tests/test_other.py tests/test_gone.py"])

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
