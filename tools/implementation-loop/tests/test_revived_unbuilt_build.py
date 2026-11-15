"""#101's path, replayed: planned, never built, failed with `ceiling` on 1.2.4, revived, refused again.

On engine 1.2.4, #101's ten failed attempts were ten `build` calls on Claude refused for the usage
limit (exit 1, empty stderr, 429 `usage_limit_reached` in the JSON result, no cost), each charged
to the infrastructure budget as a crash. No build ever answered: #101 has a plan and a worktree
but no round behind it, so its one revival sends it into `build` with nothing but the revival note,
and refusals there must not spend that try."""

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


class RevivedUnbuiltBuildTests(EngineCase):
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
        self.revive_as_101()

    def tearDown(self):
        for key in ("REFUSED_BUILD_LOG", "REFUSED_BUILD_MODELS"):
            os.environ.pop(key, None)
        super().tearDown()

    def revive_as_101(self):
        """#101's record: planned, a worktree, plan and triage spent, no round, failed `ceiling` on 1.2.4."""
        store, budget = self.engine.store, self.engine.caps.infra_budget
        store.change_budget(2, set_calls=2, set_infra=budget)
        st = store.issue(2)
        st.plan, st.worktree, st.phase = {"items": [], "zones": ["docs/a.txt"]}, str(self.tmp / "wt"), Phase.FAILED
        st.feedback = []
        st.fail = {"cause": "ceiling", "detail": f"infrastructure budget of {budget} failed attempts is spent",
                   "version": "1.2.4", "resumed": False}
        self.engine._maybe_revive(st)
        store.put(st)
        st = store.issue(2)
        self.assertEqual(st.phase, Phase.BUILD, "planned and with a worktree, the revival goes straight to the build")
        self.assertTrue(present(st.fail)["resumed"])
        self.assertEqual(store.budget(2)["infra"], 0)
        self.assertEqual(len(st.feedback), 1, "with no round behind it, the build gets only the revival note")

    def build(self):
        try:
            return self.live.call("claude", "build", self.tmp, "build #2", issue=2)
        except live.AgentFailure as e:
            self.engine._finished(2, e)
            return None

    def models(self):
        return self.log.read_text().split()

    def test_the_first_build_refused_on_fable_is_answered_on_opus(self):
        os.environ["REFUSED_BUILD_MODELS"] = present(self.settings.claude_model)
        self.assertEqual(self.build(), BUILT)
        self.assertEqual(self.models(), [self.settings.claude_model, self.settings.claude_fallback_model])
        self.assertEqual(self.engine.store.budget(2)["infra"], 0)
        self.assertEqual(self.engine.store.issue(2).phase, Phase.BUILD)

    def test_refused_first_builds_do_not_spend_the_revived_try(self):
        build = self.settings
        os.environ["REFUSED_BUILD_MODELS"] = f"{build.claude_model},{build.claude_fallback_model}"
        while not self.log.exists() or len(self.models()) <= self.engine.caps.infra_budget:
            self.engine._set_phase(2, Phase.BUILD)   # the outage is over: the issue builds again
            self.assertIsNone(self.build())
        st = self.engine.store.issue(2)
        self.assertEqual(self.engine.store.budget(2)["infra"], 0, "a refusal is charged to no budget")
        self.assertNotEqual(st.phase, Phase.FAILED, "the revived try is not burnt by a refusing vendor")
        wait = present(st.wait)
        self.assertEqual((st.phase, wait["kind"], wait["resume"]), (Phase.WAITING, "outage", "build"))
