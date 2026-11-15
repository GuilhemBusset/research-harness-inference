"""Independent refusal metering and failed-build revival checks for issue 315."""

import json
from unittest.mock import patch

from helpers import SHIP, present
from implement_loop import VERSION, live
from implement_loop.agents import FakeAgents
from implement_loop.config import Config
from implement_loop.engine import Engine
from implement_loop.graph import build_plan
from implement_loop.model import IssueState, Phase
from implement_loop.state import RunStore
from implement_loop.workspace import Workspace
from test_engine import EPIC, EngineCase


class RefusalProbe(EngineCase):
    def test_build_refusals_exceed_the_infra_budget_then_recover_on_the_fallback(self):
        self.add_work(92, "docs/result.txt", "ready")
        self.epic(92)
        store = RunStore.for_root(self.repo.work, EPIC)
        workspace = Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir)
        engine = Engine(build_plan(self.gh, EPIC, self.repo.work), store, self.gh, workspace,
                        FakeAgents(self.scripts), Config(repo="test/test"), repo_root=self.repo.work,
                        log=lambda message: None, sleep=lambda seconds: None, policy=self.policy)
        agents = live.LiveAgents("claude", self.repo.tmp / "probe-calls", workspace,
                                 retries=0, sleep=lambda seconds: None, clock=lambda: 1000)
        agents.meter = engine
        settings = agents.roles["build"]
        self.assertTrue(settings.claude_fallback_model)
        self.assertNotEqual(settings.claude_model, settings.claude_fallback_model)
        refusal = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                              "api_error_status": 429, "api_error_type": "rate_limit_error",
                              "api_error": "usage_limit_reached", "total_cost_usd": 0})
        answer = {"status": "done", "note": "ready", "tests": [], "commands": [],
                  "dispositions": [], "decisions": [], "requests": []}
        models = []
        both_refuse = True

        def spawn(cmd, *args):
            model = cmd[cmd.index("--model") + 1]
            models.append(model)
            if both_refuse or model == settings.claude_model:
                return 1, refusal, ""
            return 0, json.dumps({"is_error": False, "structured_output": answer, "total_cost_usd": 0}), ""

        with patch.object(agents, "_spawn", side_effect=spawn):
            for _ in range(engine.caps.infra_budget + 1):
                engine._set_phase(92, Phase.BUILD)
                try:
                    agents.call("claude", "build", self.repo.work, "Build the issue", issue=92)
                except live.AgentFailure as exc:
                    self.assertEqual(exc.kind, "outage")
                    engine._finished(92, exc)
                else:
                    self.fail("a vendor refusal cannot return a build answer")
                state = store.issue(92)
                self.assertEqual(state.phase, Phase.WAITING)
                self.assertIsNone(state.fail)
                wait = present(state.wait)
                self.assertEqual((wait["kind"], wait["resume"], wait["vendor"]),
                                 ("outage", "build", "claude"))
                budget = store.budget(92)
                self.assertEqual((budget["infra"], budget["calls"], budget.get("pending", 0)), (0, 0, 0))
                self.assertEqual(engine.run_calls, 0)
            self.assertEqual(models[:2], [settings.claude_model, settings.claude_fallback_model])
            both_refuse = False
            engine._set_phase(92, Phase.BUILD)
            self.assertEqual(agents.call("claude", "build", self.repo.work, "Build the issue", issue=92), answer)
        self.assertEqual(models[-1], settings.claude_fallback_model)
        self.assertEqual((store.budget(92)["infra"], store.budget(92)["calls"], engine.run_calls), (0, 1, 1))


def test_version_bump_revives_the_failed_build_once_with_fresh_budgets(tmp_path):
    assert tuple(map(int, VERSION.split("."))) > (1, 2, 7)
    engine = Engine.__new__(Engine)
    engine.store = RunStore(tmp_path / "run")
    state = IssueState(number=92, phase=Phase.FAILED, branch="loop/issue-92", worktree="work",
                       plan={"items": []}, fail={"cause": "ceiling", "version": "1.2.4", "resumed": False})
    engine.store.put(state)
    engine.store.change_budget(92, calls=30, infra=10, usd=15)
    engine._maybe_revive(state)
    assert state.phase == Phase.BUILD
    assert state.branch == "loop/issue-92" and state.worktree == "work"
    assert present(state.fail)["resumed"] is True
    budget = engine.store.budget(92)
    assert budget["infra"] == 0
    assert budget["calls"] - budget["bonus"] == 0
    assert budget["usd"] - budget["usd_bonus"] == 0
    engine._maybe_revive(state)
    assert engine.store.budget(92) == budget
    assert len([event for event in engine.store.events() if event["kind"] == "revived"]) == 1
