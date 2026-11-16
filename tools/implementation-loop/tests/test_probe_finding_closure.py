"""Regression from #48's ceiling (#103): a recurring note must not reset the progress rule."""

import shutil
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401 - puts the engine on sys.path
from implement_loop.config import Caps
from implement_loop.engine import Engine
from implement_loop.model import Phase
from implement_loop.state import RunStore


class ProbeFindingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="implement-loop-"))
        self.engine = Engine.__new__(Engine)
        self.engine.store = RunStore(self.tmp / "run")
        self.engine.caps = Caps()
        self.phases = []
        self.engine._set_phase = lambda n, phase, **kw: self.phases.append((phase, kw.get("feedback")))  # ty: ignore[invalid-assignment]  # test double
        self.engine._fail = lambda n, cause, detail: self.phases.append((Phase.FAILED, cause))  # ty: ignore[invalid-assignment]  # test double

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_recurring_note_does_not_reset_the_progress_rule(self):
        note = "These commands were not kept, so they did not run: [...]"
        failure = ["open finding E48-1-1 still reproduces: Qualify the blanket cache-invalidation advice"]
        for notes in ([note], [], [note]):
            self.engine._round_failed(48, failure, ["E48-1-1"], notes=notes)
        self.assertEqual(self.engine.store.issue(48).unproductive, 2)
        self.assertEqual(self.phases[0], (Phase.BUILD, [note] + failure), "the note still reaches the builder")
        self.assertEqual(self.phases[-1][0], Phase.PLAN, "two unchanged rounds bring a replan")


if __name__ == "__main__":
    unittest.main()
