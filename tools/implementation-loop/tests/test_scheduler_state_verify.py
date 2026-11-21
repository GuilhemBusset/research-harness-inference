import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import HAS_PYTEST, TempRepo, git
from implement_loop import scheduler, verify
from implement_loop.model import CheckSpec, IssueState, Phase
from implement_loop.state import LockHeld, RepoClaims, RunLock, RunStore


class SchedulerTests(unittest.TestCase):
    def test_overlap_is_prefix_aware(self):
        self.assertTrue(scheduler.overlaps("packages/01/", "packages/01/labs/a.html"))
        self.assertTrue(scheduler.overlaps("a/b.txt", "a/b.txt"))
        self.assertFalse(scheduler.overlaps("packages/01", "packages/010/x"))
        self.assertFalse(scheduler.overlaps("a/b.txt", "a/b.txt.bak"))

    def test_shared_files_serialize_but_nested_readmes_do_not(self):
        self.assertTrue(scheduler.is_shared("README.md"))
        self.assertTrue(scheduler.is_shared(".github/workflows/ci.yml"))
        self.assertFalse(scheduler.is_shared("packages/01-fundamentals/README.md"))
        self.assertTrue(scheduler.conflicts(["AGENTS.md", "x/a"], [["README.md", "y/b"]]))
        self.assertFalse(scheduler.conflicts(["x/a"], [["y/b"]]))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = RunStore(self.tmp / "run")

    def test_state_round_trips_and_events_are_sequenced(self):
        st = IssueState(number=7, phase=Phase.WAITING, calls=2, infra=1, grants=[{"path": "a", "protected": True}],
                        wait={"kind": "outage", "resume": "examine", "vendor": "codex"},
                        obligations=[{"item": "D2", "kind": "deferred", "detail": "#9", "issue": 9}])
        self.store.put(st)
        self.store.event("one", issue=7)
        self.store.event("two")
        again = RunStore(self.tmp / "run")
        self.assertEqual(again.issue(7).phase, Phase.WAITING)
        self.assertEqual(again.issue(7).grants, [{"path": "a", "protected": True}])
        self.assertEqual((again.issue(7).calls, again.issue(7).infra), (2, 1), "budgets persist across runs")
        self.assertEqual(again.issue(7).to_dict(), st.to_dict())
        self.assertEqual([e["seq"] for e in again.events()], [1, 2])
        again.event("three")
        self.assertEqual(again.events()[-1]["seq"], 3)

    def test_lock_blocks_a_live_holder_and_takes_over_a_dead_one(self):
        lock_path = self.tmp / "run" / "lock"
        with RunLock(lock_path), self.assertRaises(LockHeld):
            RunLock(lock_path).__enter__()
        lock_path.write_text(json.dumps({"pid": 2 ** 22 + 12345, "host": socket.gethostname()}))
        with RunLock(lock_path):
            self.assertEqual(json.loads(lock_path.read_text())["pid"], os.getpid())
        self.assertFalse(lock_path.exists())

    def test_repo_claims_are_exclusive_per_resource(self):
        repo = TempRepo()
        try:
            claims = RepoClaims(repo.work)
            self.assertTrue(claims.acquire("merge-queue", "run-1"))
            self.assertTrue(claims.acquire("merge-queue", "run-1"))
            self.assertFalse(claims.acquire("merge-queue", "run-2"))
            claims.release("merge-queue", "run-1")
            self.assertTrue(claims.acquire("merge-queue", "run-2"))
        finally:
            repo.cleanup()


class LegacyStateTests(unittest.TestCase):
    """State written by the ADR 0008 and ADR 0011 engines reads with the new phases; a parked
    issue or an earlier design is kept aside for `Engine._migrate`, never dropped."""

    def test_state_from_the_stop_and_ask_engine_reads_with_the_new_outcomes(self):
        stopped = IssueState.from_dict({"number": 21, "phase": "needs_human", "reason": "a design dispute"})
        self.assertEqual(stopped.phase, Phase.PENDING)
        assert stopped.wait is not None
        self.assertEqual((stopped.wait["kind"], stopped.wait["park_kind"], stopped.wait["reason"]),
                         ("legacy", "exhausted", "a design dispute"))
        accepted = IssueState.from_dict({"number": 22, "phase": "accepted"})
        self.assertEqual((accepted.phase, accepted.wait), (Phase.DELIVERED, None))

    def test_old_phases_map_to_the_new_ones_and_old_fields_are_dropped(self):
        for old, new in (("design", Phase.PLAN), ("designed", Phase.PLANNED), ("checks", Phase.BUILD),
                         ("implement", Phase.BUILD), ("review", Phase.EXAMINE), ("verify", Phase.VERIFY),
                         ("land", Phase.LAND), ("done", Phase.DONE)):
            with self.subTest(phase=old):
                st = IssueState.from_dict({"number": 5, "phase": old, "attempts": 3, "fix_rounds": 1,
                                           "reviewed_sha": "abc", "checks_sha": "def", "redesigns": 1})
                self.assertEqual((st.phase, st.wait), (new, None))
                self.assertFalse(hasattr(st, "attempts"))

    def test_a_parked_issue_keeps_why_and_where_for_the_migration(self):
        st = IssueState.from_dict({"number": 23, "phase": "parked", "park_kind": "engine_error",
                                   "resume_phase": "review", "reason": "a KeyError", "parked_version": "0.3.0",
                                   "branch": "feat/x", "head_sha": "abc"})
        self.assertEqual((st.phase, st.branch, st.head_sha), (Phase.PENDING, "feat/x", "abc"))
        self.assertEqual(st.wait, {"kind": "legacy", "park_kind": "engine_error", "resume_phase": "review",
                                   "reason": "a KeyError", "parked_version": "0.3.0"})

    def test_an_earlier_design_and_its_locks_are_kept_for_the_migration(self):
        design = {"files": ["docs/a.txt"], "check_files": ["checks/old.sh"], "checks": []}
        st = IssueState.from_dict({"number": 24, "phase": "implement", "design": design,
                                   "locked": {"checks/old.sh": "digest"}})
        self.assertEqual(st.phase, Phase.BUILD)
        assert st.wait is not None
        self.assertEqual((st.wait["kind"], st.wait["design"], st.wait["locked"]),
                         ("legacy", design, {"checks/old.sh": "digest"}))
        self.assertEqual(st.to_dict()["wait"]["design"], design, "it survives a save until the engine migrates it")

    def test_state_with_the_locks_zones_and_requests_of_an_earlier_engine_still_reads(self):
        st = IssueState.from_dict({"number": 25, "phase": "verify", "tests": ["tests/test_a.py"],
                                   "locked": {"tests/test_a.py": "digest"}, "manifest": {"*": ["tests/test_a.py::t"]},
                                   "dispositions": {"tests/test_a.py::u": "owner-run"}, "reverted": ["tests/test_a.py"],
                                   "last_ran": {"*": ["tests/test_a.py::t"]},
                                   "plan": {"items": [], "zones": ["docs/"], "notes": ""},
                                   "grants": [{"path": "docs/", "source": "plan", "protected": False}],
                                   "requests": [{"id": "R25-1", "kind": "test_change", "status": "pending"}]})
        self.assertEqual((st.phase, st.wait, st.plan, st.grants[0]["path"], st.requests[0]["kind"]),
                         (Phase.VERIFY, None, {"items": [], "zones": ["docs/"], "notes": ""}, "docs/", "test_change"))
        for gone in ("tests", "locked", "manifest", "dispositions", "reverted", "last_ran"):
            self.assertNotIn(gone, st.to_dict())


class MigrationTests(unittest.TestCase):
    """`Engine._migrate`: what an earlier engine left becomes the starting point of a plan."""

    def test_a_parked_design_becomes_the_builders_tests_and_feedback(self):
        from helpers import SHIP, issue_body
        from implement_loop.agents import FakeAgents, Script
        from implement_loop.config import Caps, Config
        from implement_loop.engine import Engine
        from implement_loop.github import FakeGitHub
        from implement_loop.graph import build_plan
        from implement_loop.workspace import Workspace
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        gh = FakeGitHub()
        gh.add_issue(2, "Issue 2", issue_body("feat/issue-2", ["`docs/a.txt` contains `alpha`."], ["It passes."]))
        gh.add_issue(1, "Epic", "An epic.\n", subs=(2,))
        store = RunStore(repo.tmp / "run")
        store.state["issues"]["2"] = {
            "number": 2, "phase": "parked", "park_kind": "exhausted", "reason": "the design was impossible",
            "design": {"files": ["docs/a.txt"], "check_files": ["checks/old_2.py"], "checks": []},
            "locked": {"checks/old_2.py": "x", "checks/conftest.py": "y"}}
        store.save()
        engine = Engine(build_plan(gh, 1, repo.work), store, gh,
                        Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir),
                        FakeAgents({2: Script(files={"docs/a.txt": "alpha"})}), Config(repo="t/t", caps=Caps()),
                        repo_root=repo.work, log=lambda m: None, sleep=lambda s: None)
        engine._reconcile()
        st = store.issue(2)
        self.assertEqual((st.phase, st.wait, st.grants, st.requests), (Phase.PENDING, None, [], []))
        self.assertTrue(any("checks/conftest.py, checks/old_2.py" in f and "They are yours now" in f
                            for f in st.feedback), "the builder takes over the checks it locked")
        self.assertTrue(any("the design was impossible" in f for f in st.feedback))
        self.assertEqual([e["park_kind"] for e in store.events() if e["kind"] == "migrated"], ["exhausted"])


class StopTests(unittest.TestCase):
    def test_only_a_stop_older_than_the_invocation_is_cleared(self):
        import time
        store = RunStore(Path(tempfile.mkdtemp()) / "run")
        (store.dir / "STOP").write_text("stop\n")
        self.assertFalse(store.clear_stop(older_than=time.time() - 60), "a stop asked after the run started stays")
        self.assertTrue(store.stop_requested())
        self.assertTrue(store.clear_stop(older_than=time.time() + 60))
        self.assertFalse(store.stop_requested())


class UncommittedTests(unittest.TestCase):
    def test_a_modified_file_listed_first_keeps_its_whole_path(self):
        # the porcelain line " M setup/package.json" was stripped and cut to "etup/package.json"
        from implement_loop.workspace import Workspace
        repo = TempRepo()
        try:
            wt = repo.work
            git("switch", "-q", "-c", "feat/x", cwd=wt)
            (wt / "setup").mkdir()
            (wt / "setup" / "package.json").write_text("{}\n")
            git("add", "-A", cwd=wt)
            git("commit", "-q", "-m", "seed", cwd=wt)
            (wt / "setup" / "package.json").write_text('{"x": 1}\n')
            (wt / "new file.txt").write_text("x\n")
            ws = Workspace(wt, 1, Path("ship.sh"), base_dir=repo.loop_dir)
            self.assertEqual(sorted(ws.uncommitted(wt)), ["new file.txt", "setup/package.json"])
        finally:
            repo.cleanup()

    def test_changed_files_keep_non_ascii_paths_whole(self):
        # git quotes such paths in plain output, so a zone naming one never matched it
        from implement_loop.workspace import Workspace
        repo = TempRepo()
        try:
            wt = repo.work
            base = git("rev-parse", "HEAD", cwd=wt)
            git("switch", "-q", "-c", "feat/x", cwd=wt)
            (wt / "assets").mkdir()
            (wt / "assets" / "théorie λ.txt").write_text("x\n")
            git("add", "-A", cwd=wt)
            git("commit", "-q", "-m", "add", cwd=wt)
            (wt / "notes é.txt").write_text("y\n")
            ws = Workspace(wt, 1, Path("ship.sh"), base_dir=repo.loop_dir)
            self.assertEqual(ws.changed_files(wt, base), ["assets/théorie λ.txt", "notes é.txt"])
        finally:
            repo.cleanup()


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "checks").mkdir()
        (self.root / "checks" / "c.sh").write_text("test \"$(cat out.txt)\" = ok\n")
        (self.root / "out.txt").write_text("ok\n")
        self.work = self.root / ".work"

    def spec(self, cmd="bash checks/c.sh", **kw):
        return CheckSpec(criterion="A1", kind="command", command=cmd, **kw)

    def test_command_evidence_records_exit_and_head(self):
        ev, _ = verify.run_check(self.root, self.spec(), "abc123", self.work)
        self.assertTrue(ev.passed)
        self.assertEqual((ev.exit_code, ev.head_sha), (0, "abc123"))
        (self.root / "out.txt").write_text("no\n")
        ev, _ = verify.run_check(self.root, self.spec(), "abc123", self.work)
        self.assertFalse(ev.passed)

    def test_missing_cwd_fails_cleanly(self):
        ev, _ = verify.run_check(self.root, self.spec(cwd="nope"), "abc", self.work)
        self.assertFalse(ev.passed)
        self.assertIn("does not exist", ev.output_tail)


@unittest.skipUnless(HAS_PYTEST, "pytest is not installed")
class PytestVerifyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "tests").mkdir()
        self.test_file = self.root / "tests" / "test_x.py"

    def test_pytest_wrapped_in_a_script_is_still_accounted(self):
        (self.root / "run.sh").write_text(f"{sys.executable} -m pytest -q tests/test_x.py\n")
        self.test_file.write_text("import pytest\n\ndef test_a():\n    pytest.skip('later')\n")
        ok, _, problems = verify.verify(self.root, [CheckSpec("A1", "command", "bash run.sh")], "sha", self.root / ".w")
        self.assertFalse(ok)
        self.assertTrue(any("unexpected skip" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
