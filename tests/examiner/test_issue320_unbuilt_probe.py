"""Independent recovery checks for a planned issue whose first build was refused."""

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


def test_new_version_restores_the_unbuilt_issue_once(tmp_path):
    assert tuple(map(int, VERSION.split("."))) > (1, 2, 13)
    engine = Engine.__new__(Engine)
    engine.store = RunStore(tmp_path / "run")
    state = IssueState(number=101, phase=Phase.FAILED, branch="loop/issue-101", worktree="work",
                       plan={"items": [], "zones": ["docs/result.txt"]}, feedback=[],
                       fail={"cause": "ceiling", "version": "1.2.4", "resumed": False})
    engine.store.change_budget(101, calls=2, infra=10)
    engine._maybe_revive(state)
    assert state.phase == Phase.BUILD
    assert (state.branch, state.worktree) == ("loop/issue-101", "work")
    assert len(state.feedback) == 1
    assert present(state.fail)["resumed"] is True
    budget = engine.store.budget(101)
    assert (budget["calls"], budget["bonus"], budget["infra"]) == (2, 2, 0)
    state.phase = Phase.FAILED
    engine._maybe_revive(state)
    assert state.phase == Phase.FAILED
    assert engine.store.budget(101) == budget
    assert len([e for e in engine.store.events() if e["kind"] == "revived"]) == 1


class UnbuiltRecoveryProbe(EngineCase):
    def test_refusals_preserve_the_revived_budget_until_a_build_answer(self):
        self.add_work(101, "docs/result.txt", "ready")
        self.epic(101)
        root = self.repo.work
        store = RunStore.for_root(root, EPIC)
        workspace = Workspace(root, EPIC, SHIP, base_dir=self.repo.loop_dir)
        engine = Engine(build_plan(self.gh, EPIC, root), store, self.gh, workspace,
                        FakeAgents(self.scripts), Config(repo="test/test"), repo_root=root,
                        log=lambda _: None, sleep=lambda _: None, policy=self.policy)
        state = store.issue(101)
        state.phase, state.worktree = Phase.FAILED, str(root)
        state.plan, state.feedback = {"items": [], "zones": ["docs/result.txt"]}, []
        state.fail = {"cause": "ceiling", "version": "1.2.4", "resumed": False}
        store.change_budget(101, set_calls=2, set_infra=engine.caps.infra_budget)
        engine._maybe_revive(state)
        store.put(state)
        self.assertEqual((state.phase, len(state.feedback)), (Phase.BUILD, 1))
        agents = live.LiveAgents("claude", self.repo.tmp / "probe-calls", workspace,
                                 retries=0, sleep=lambda _: None, clock=lambda: 1000)
        agents.meter = engine
        settings = agents.roles["build"]
        refusal = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                              "api_error_status": 429, "api_error_type": "rate_limit_error",
                              "api_error": "usage_limit_reached", "total_cost_usd": 0})
        answer = {"status": "done", "note": "ready", "tests": [], "commands": [],
                  "dispositions": [], "decisions": [], "requests": []}
        models = []
        refuse_all = True

        def spawn(cmd, *args):
            model = cmd[cmd.index("--model") + 1]
            models.append(model)
            if refuse_all or model == settings.claude_model:
                return 1, refusal, ""
            return 0, json.dumps({"type": "result", "is_error": False, "structured_output": answer}), ""

        with patch.object(agents, "_spawn", side_effect=spawn):
            for _ in range(engine.caps.infra_budget + 1):
                with self.assertRaises(live.AgentFailure) as refused:
                    agents.call("claude", "build", root, "Build the issue", issue=101)
                self.assertEqual(refused.exception.kind, "outage")
                engine._finished(101, refused.exception)
                state = store.issue(101)
                wait = present(state.wait)
                self.assertEqual((state.phase, wait["kind"], wait["resume"], wait["vendor"]),
                                 (Phase.WAITING, "outage", "build", "claude"))
                budget = store.budget(101)
                self.assertEqual((budget["infra"], budget["calls"], budget["pending"]), (0, 2, 0))
                self.assertEqual(engine.run_calls, 0)
                engine._maybe_resume(state)
                self.assertEqual(state.phase, Phase.BUILD)
                store.put(state)
            self.assertEqual(models[:2], [settings.claude_model, settings.claude_fallback_model])
            refuse_all = False
            self.assertEqual(agents.call("claude", "build", root, "Build the issue", issue=101), answer)
        self.assertEqual(models[-1], settings.claude_fallback_model)
        budget = store.budget(101)
        self.assertEqual((budget["infra"], budget["calls"], budget["pending"], engine.run_calls), (0, 3, 0, 1))
