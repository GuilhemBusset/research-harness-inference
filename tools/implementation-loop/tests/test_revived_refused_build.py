"""#90's path, replayed: failed with `ceiling` on 1.2.4, revived by a new engine version, refused again.

On engine 1.2.4, #90's ten failed attempts were ten `build` calls on Claude refused for the usage
limit (exit 1, empty stderr, 429 `usage_limit_reached` in the JSON result, no cost), each charged
to the infrastructure budget as a crash. A failed issue is revived only once: here the revival
puts #90 back in `build` with a fresh budget, and refusals there must not spend that one try."""

import os
import stat
import sys
import tempfile
from pathlib import Path

from helpers import SHIP, present
from implement_loop import live
from implement_loop.agents import FakeAgents
from implement_loop.config import Caps, Config
from implement_loop.engine import Engine
from implement_loop.graph import build_plan
from implement_loop.model import Phase
from implement_loop.state import RunStore
from implement_loop.workspace import Workspace
from test_engine import EPIC, EngineCase
from test_refused_build import BUILT, FAKE, StubWorkspace


class RevivedRefusedBuildTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        self.engine = Engine(build_plan(self.gh, EPIC, self.repo.work), RunStore.for_root(self.repo.work, EPIC),
                             self.gh, Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir),
                             FakeAgents(self.scripts), Config(repo="t/t", caps=Caps(ci_poll_s=0, queue_poll_s=0)),
                             repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None, policy=self.policy)
        self.tmp = Path(tempfile.mkdtemp())
        claude = self.tmp / "claude"
        claude.write_text(FAKE.format(python=sys.executable, answer=repr(BUILT)))
        claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "models.txt"
        os.environ["REFUSED_BUILD_LOG"] = str(self.log)
        roles = live.load_roles()
        self.settings = roles["build"]   # the checked-in settings: Fable, then Opus
        self.live = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp), roles=roles,
                                    claude_bin=str(claude), codex_bin=str(claude), sleep=lambda s: None)
        self.live.meter = self.engine
        self.revive_as_90()

    def tearDown(self):
        for key in ("REFUSED_BUILD_LOG", "REFUSED_BUILD_MODELS"):
            os.environ.pop(key, None)
        super().tearDown()

    def revive_as_90(self):
        """#90's record: planned, a worktree holding its work, failed `ceiling` on 1.2.4, infra spent."""
        store, budget = self.engine.store, self.engine.caps.infra_budget
        store.change_budget(2, set_calls=1, set_infra=budget)
        st = store.issue(2)
        st.plan, st.worktree, st.phase = {"items": [], "zones": ["docs/a.txt"]}, str(self.tmp / "wt"), Phase.FAILED
        st.fail = {"cause": "ceiling", "detail": f"infrastructure budget of {budget} failed attempts is spent",
                   "version": "1.2.4", "resumed": False}
        self.engine._maybe_revive(st)
        store.put(st)
        st = store.issue(2)
        self.assertEqual(st.phase, Phase.BUILD, "the revival resumes the build on the branch that keeps the work")
        self.assertTrue(present(st.fail)["resumed"])
        self.assertEqual(store.budget(2)["infra"], 0)
        self.assertEqual(len([e for e in store.events() if e["kind"] == "revived" and e.get("issue") == 2]), 1)

    def build(self):
        try:
            return self.live.call("claude", "build", self.tmp, "build #2", issue=2)
        except live.AgentFailure as e:
            self.engine._finished(2, e)
            return None

    def models(self):
        return self.log.read_text().split()

    def test_the_revived_build_refused_on_fable_is_answered_on_opus(self):
        os.environ["REFUSED_BUILD_MODELS"] = present(self.settings.claude_model)
        self.assertEqual(self.build(), BUILT)
        self.assertEqual(self.models(), [self.settings.claude_model, self.settings.claude_fallback_model])
        self.assertEqual(self.engine.store.budget(2)["infra"], 0)
        self.assertEqual(self.engine.store.issue(2).phase, Phase.BUILD)

    def test_refusals_after_the_revival_do_not_spend_its_one_try(self):
        build = self.settings
        os.environ["REFUSED_BUILD_MODELS"] = f"{build.claude_model},{build.claude_fallback_model}"
        while not self.log.exists() or len(self.models()) <= self.engine.caps.infra_budget:
            self.engine._set_phase(2, Phase.BUILD)   # the outage is over: the issue resumes its build
            self.assertIsNone(self.build())
        st = self.engine.store.issue(2)
        self.assertEqual(self.engine.store.budget(2)["infra"], 0, "a refusal is charged to no budget")
        self.assertNotEqual(st.phase, Phase.FAILED, "the revived try is not burnt by a refusing vendor")
        wait = present(st.wait)
        self.assertEqual((st.phase, wait["kind"], wait["resume"]), (Phase.WAITING, "outage", "build"))
