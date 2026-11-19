"""Regression for the engine fault a25bef17ec (#243): the examiner named a probe file that no file
system can create, and the lint of that probe (`Engine._probe_problem`, called by `_step_examine`)
raised `OSError` where it writes the probe to a temporary directory. Such a probe is refused like
any other the lint refuses, and the examination carries on."""

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest import mock

import test_engine
from helpers import present
from implement_loop import engine as loaded
from implement_loop.agents import FakeAgents, Probe
from implement_loop.model import Phase
from test_engine import EPIC, EngineCase


def engine_beside_these_tests():
    """`Engine` from the `engine.py` beside these tests, which is the one they judge. The verifier's
    pytest plugin belongs to the engine that runs the loop, and loading it imports that engine's
    `implement_loop`. It drops the package again only in the pytest that records, not in an xdist
    worker, so the tests of `scripts/check.sh engine-tests` would find there the `engine.py` of the
    running engine: the one with the fault, while this repair is built."""
    mine = Path(__file__).resolve().parent.parent / "implement_loop" / "engine.py"
    if Path(present(loaded.__file__)).resolve() == mine:
        return loaded.Engine
    spec = present(importlib.util.spec_from_file_location("implement_loop.engine_beside_these_tests", mine))
    module = importlib.util.module_from_spec(spec)
    sys.modules[present(spec.name)] = module
    present(spec.loader).exec_module(module)
    return module.Engine


Engine = engine_beside_these_tests()

# One name longer than any Linux file system takes (255 bytes); every existing rule of the lint accepts it.
UNWRITABLE = "checks/" + "x" * 300 + ".sh"
CONTENT = "#!/usr/bin/env bash\ntest -s docs/a.txt\n"


class NamesAnUnwritableProbe(FakeAgents):
    """The fake examiner, which at its first examination also writes a probe at `UNWRITABLE` and
    makes its blocking finding stand on it."""

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        result = super().examine(vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since)
        if since is None:
            result.probes.append(Probe(UNWRITABLE, "D1", f"bash {UNWRITABLE}", content=CONTENT))
            for finding in result.findings:
                finding.probe = UNWRITABLE
        return result


class UnwritableProbeTests(EngineCase):
    def test_the_file_system_refuses_the_probes_path(self):
        # what makes the two tests below a reproduction of the fault on this host
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / UNWRITABLE
            target.parent.mkdir(parents=True)
            with self.assertRaises(OSError):
                target.write_text(CONTENT)

    def test_the_lint_refuses_it_instead_of_raising(self):
        engine = Engine.__new__(Engine)
        engine.repair = False
        problem = engine._probe_problem(Probe(UNWRITABLE, "D1", f"bash {UNWRITABLE}", content=CONTENT))
        self.assertTrue(problem, "a reason the probe is refused for, not an exception and not a pass")
        self.assertNotIn(tempfile.gettempdir(), problem, "the reason is the engine's own words, not the system's")
        self.assertIsNone(engine._probe_problem(Probe("checks/probe.sh", "D1", "bash checks/probe.sh", content=CONTENT)),
                          "a probe the file system takes still passes the lint")

    def test_an_examination_that_names_one_carries_on(self):
        self.add_work(2, "docs/a.txt", "alpha", findings=1)
        self.epic(2)
        with mock.patch.object(test_engine, "Engine", Engine):   # the engine this module judges
            self.run_engine(agents=NamesAnUnwritableProbe(self.scripts))

        self.assertEqual(self.events("step_crashed"), [], "the examiner's mistake is not an engine fault")
        examined = self.events("examined", 2)[0]
        self.assertIn(UNWRITABLE, [r["file"] for r in examined["rejected"]])
        self.assertTrue(any(status == "open" for status in examined["findings"].values()),
                        "the finding that named it still blocks, on its own reproduction")
        self.assertEqual(self.phase(2), Phase.DONE, "and closes once that reproduction passes")
        self.assertEqual(self.gh.issues[EPIC]["state"], "closed")
