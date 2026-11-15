"""#57's path, replayed: failed with `ceiling` on 1.2.4 while planning, revived, refused again.

On engine 1.2.4, #57's ten failed attempts were ten `plan` calls on Claude refused for the usage
limit (exit 1, empty stderr, 429 `usage_limit_reached` in the JSON result, no cost), each charged
to the infrastructure budget as a crash. #57 had no plan and no worktree, so its one revival puts it
back in `pending`, to plan again with a fresh budget, and refusals there must not spend that try."""

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
from test_refused_build import FAKE, StubWorkspace

PLANNED = {"items": [], "zones": ["docs/a.txt"], "decisions": [], "notes": "planned"}


class RevivedRefusedPlanTests(EngineCase):
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
        claude.write_text(FAKE.format(python=sys.executable, answer=repr(PLANNED)))
        claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "models.txt"
        os.environ["REFUSED_BUILD_LOG"] = str(self.log)
        roles = live.load_roles()
        self.settings = roles["plan"]   # the checked-in settings: Fable, then Opus
        self.live = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp), roles=roles,
                                    claude_bin=str(claude), codex_bin=str(claude), sleep=lambda s: None)
        self.live.meter = self.engine
        self.revive_as_57()

    def tearDown(self):
        for key in ("REFUSED_BUILD_LOG", "REFUSED_BUILD_MODELS"):
            os.environ.pop(key, None)
        super().tearDown()

    def revive_as_57(self):
        """#57's record: never planned, no worktree, failed `ceiling` on 1.2.4, infra spent."""
        store, budget = self.engine.store, self.engine.caps.infra_budget
        store.change_budget(2, set_infra=budget)
        st = store.issue(2)
        st.plan, st.worktree, st.phase = None, None, Phase.FAILED
        st.fail = {"cause": "ceiling", "detail": f"infrastructure budget of {budget} failed attempts is spent",
                   "version": "1.2.4", "resumed": False}
        self.engine._maybe_revive(st)
        store.put(st)
        st = store.issue(2)
        self.assertEqual(st.phase, Phase.PENDING, "with no plan, the revival starts the issue over")
        self.assertTrue(present(st.fail)["resumed"])
        self.assertEqual(store.budget(2)["infra"], 0)
        self.engine._set_phase(2, Phase.PLAN)   # what the step loop does with a pending issue it may start

    def plan(self):
        try:
            return self.live.call("claude", "plan", self.tmp, "plan #2", issue=2)
        except live.AgentFailure as e:
            self.engine._finished(2, e)
            return None

    def models(self):
        return self.log.read_text().split()

    def test_the_revived_plan_refused_on_fable_is_answered_on_opus(self):
        os.environ["REFUSED_BUILD_MODELS"] = present(self.settings.claude_model)
        self.assertEqual(self.plan(), PLANNED)
        self.assertEqual(self.models(), [self.settings.claude_model, self.settings.claude_fallback_model])
        self.assertEqual(self.engine.store.budget(2)["infra"], 0)
        self.assertEqual(self.engine.store.issue(2).phase, Phase.PLAN)

    def test_refused_plans_after_the_revival_do_not_spend_its_one_try(self):
        plan = self.settings
        os.environ["REFUSED_BUILD_MODELS"] = f"{plan.claude_model},{plan.claude_fallback_model}"
        while not self.log.exists() or len(self.models()) <= self.engine.caps.infra_budget:
            self.engine._set_phase(2, Phase.PLAN)   # the outage is over: the issue plans again
            self.assertIsNone(self.plan())
        st = self.engine.store.issue(2)
        self.assertEqual(self.engine.store.budget(2)["infra"], 0, "a refusal is charged to no budget")
        self.assertNotEqual(st.phase, Phase.FAILED, "the revived try is not burnt by a refusing vendor")
        wait = present(st.wait)
        self.assertEqual((st.phase, wait["kind"], wait["resume"]), (Phase.WAITING, "outage", "plan"))
