"""The supervisor resumes runs by itself, within fixed bounds (ADR 0012)."""

import json
import unittest
from types import SimpleNamespace

from helpers import TempRepo
from implement_loop.engine import Engine
from implement_loop.model import IssueState, Phase
from implement_loop.state import FaultOutbox, RunStore, runs_root
from implement_loop.supervisor import BACKOFF_S, Supervisor

ROOT = 1


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.root = self.repo.work
        self.calls: list[list[str]] = []
        self.slept: list[float] = []
        self.reachable = {"claude": True, "codex": True}

    def tearDown(self):
        self.repo.cleanup()

    def put(self, n, phase, **fields):
        store = RunStore.for_root(self.root, ROOT)
        st = IssueState(number=n, phase=phase, **fields)
        store.put(st)

    def sup(self, script, **kw):
        def run(args):
            self.calls.append(args)
            script(args)
            return 0
        return Supervisor(self.root, ROOT, "claude", run=run, reachable=lambda v: self.reachable[v],
                          pull=lambda: "main checkout updated", sleep=self.slept.append, log=lambda m: None, **kw)

    def test_finishes_when_every_issue_is_terminal(self):
        self.put(2, Phase.PENDING)
        out = self.sup(lambda args: self.put(2, Phase.DONE)).supervise()
        self.assertTrue(out.finished)
        self.assertEqual(out.runs, 1)
        self.assertEqual(self.calls, [["run", "1", "--operator", "claude", "--yes"]])

    def test_an_outage_is_waited_out_before_the_next_run(self):
        self.put(2, Phase.WAITING, wait={"kind": "outage", "vendor": "codex", "resume": "examine"})
        self.reachable["codex"] = False
        state = {"n": 0}

        def script(args):
            self.put(2, Phase.DONE)

        def sleep(s):
            self.slept.append(s)
            state["n"] += 1
            if state["n"] == 2:
                self.reachable["codex"] = True
        sup = self.sup(script)
        sup.sleep = sleep
        out = sup.supervise()
        self.assertTrue(out.finished)
        self.assertEqual(len(self.calls), 1, "no run while the vendor is unreachable")
        self.assertGreaterEqual(len(self.slept), 2)

    def test_one_repair_run_per_fault_then_the_root_resumes(self):
        self.put(2, Phase.WAITING, wait={"kind": "engine", "resume": "build", "version": "1.0.0"})
        faults = runs_root(self.root) / "faults.json"
        faults.write_text(json.dumps({"abc123": {"filed": 77, "runs": [ROOT], "count": 1, "issues": [2]}}))
        runs = []

        def script(args):
            runs.append(args)
            if args[1] == "1" and len(runs) >= 2:
                self.put(2, Phase.DONE)
        out = self.sup(script).supervise()
        self.assertTrue(out.finished)
        self.assertEqual(self.calls[0], ["run", "77", "--operator", "claude", "--yes", "--repair"])
        self.assertEqual([c for c in self.calls if "--repair" in c], [self.calls[0]], "one repair per fingerprint")
        self.assertEqual(out.repairs, ["abc123"])

    def engine(self) -> Engine:
        """Enough of an engine to fail an issue or file a fault for it, with no run around it."""
        engine = Engine.__new__(Engine)
        engine.store, engine.faults, engine.repo_root = RunStore.for_root(self.root, ROOT), FaultOutbox(self.root), self.root
        engine.plan, engine.log = SimpleNamespace(root=ROOT), lambda m: None  # ty: ignore[invalid-assignment]  # test double
        engine._set_phase = engine._wait = lambda *a, **k: None  # ty: ignore[invalid-assignment]  # test double
        return engine

    def filed(self, stall: bool) -> None:
        """Issue #2's fault, filed as #77: a stall, as an earlier engine wrote it (`_fail` files none
        now, but `faults.json` keeps the ones it filed), or an engine exception (`_engine_fault`)."""
        self.put(2, Phase.BUILD)
        engine = self.engine()
        if stall:
            engine.faults.record("5ta11ed000", "implement loop: #2 not delivered (ceiling)", "body", issue=2, run=ROOT)
        else:
            try:
                raise KeyError("a crashed step")
            except KeyError as exc:
                engine._engine_fault(2, exc)
        [(fingerprint, _)] = engine.faults.pending()
        engine.faults.mark_reported(fingerprint, 77)

    def test_an_issue_that_fails_files_no_fault_and_stays_in_the_journal(self):
        self.put(2, Phase.BUILD)
        engine = self.engine()
        engine._fail(2, "ceiling", "the issue spent its call ceiling")
        self.assertEqual(engine.faults.pending(), [], "a stall is no loop fault")
        [event] = [e for e in engine.store.events() if e["kind"] == "failed"]
        self.assertEqual((event["issue"], event["cause"], event["detail"]),
                         (2, "ceiling", "the issue spent its call ceiling"))
        self.assertEqual((engine.store.issue(2).fail or {}).get("cause"), "ceiling")

    def supervise_filed(self, stall: bool):
        self.filed(stall)
        out = self.sup(lambda args: self.put(2, Phase.DONE) if "--repair" not in args else None).supervise()
        self.assertTrue(out.finished)
        return [c for c in self.calls if "--repair" in c]

    def test_a_stall_fault_gets_no_repair_run(self):
        self.assertEqual(self.supervise_filed(stall=True), [], "it stays filed for the operator")

    def test_an_engine_exception_still_gets_its_repair_run(self):
        self.assertEqual(self.supervise_filed(stall=False), [["run", "77", "--operator", "claude", "--yes", "--repair"]])

    def test_bounds_stop_a_supervision_that_never_settles(self):
        self.put(2, Phase.WAITING, wait={"kind": "ci", "resume": "land"})
        out = self.sup(lambda args: None, max_runs=50).supervise()
        self.assertFalse(out.finished)
        self.assertEqual(out.runs, 10, "the bound can be lowered, never raised")
        self.assertIn(2, out.waiting)

    def test_without_a_deadline_time_never_stops_a_supervision(self):
        self.put(2, Phase.WAITING, wait={"kind": "ci", "resume": "land"})
        now = [0.0]

        def a_long_run(args):
            now[0] += 30 * 3600   # each run outlasts any day
        sup = self.sup(a_long_run, clock=lambda: now[0])
        out = sup.supervise()
        self.assertIsNone(sup._deadline)
        self.assertEqual(out.runs, 10, "only the bound on runs ended it")
        self.assertIn("runs", out.reason)
        self.assertEqual(self.slept, [BACKOFF_S[min(quiet, len(BACKOFF_S) - 1)] for quiet in range(1, 11)],
                         "the backoff is never cut short by a deadline")

    def test_a_deferred_item_keeps_the_supervision_going_until_it_reopens(self):
        self.put(2, Phase.DELIVERED, obligations=[{"item": "D2", "kind": "deferred", "detail": "#9", "issue": 9}])
        state = {"n": 0}

        def script(args):
            state["n"] += 1
            if state["n"] == 2:
                self.put(2, Phase.DONE)
        out = self.sup(script).supervise()
        self.assertTrue(out.finished)
        self.assertEqual(out.runs, 2)

    def test_an_issue_failed_on_this_engine_version_is_settled(self):
        from implement_loop import VERSION
        self.put(2, Phase.FAILED, fail={"cause": "no_progress", "version": VERSION})
        out = self.sup(lambda args: None).supervise()
        self.assertTrue(out.finished)
        self.assertIn(2, out.failed)


if __name__ == "__main__":
    unittest.main()
