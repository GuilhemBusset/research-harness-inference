"""A revived plan keeps its fresh budget through usage-limit refusals."""

from __future__ import annotations

import json
from unittest.mock import patch

from helpers import SHIP
from implement_loop import VERSION, live
from implement_loop.agents import AgentFailure, FakeAgents
from implement_loop.config import Config
from implement_loop.engine import Engine
from implement_loop.graph import build_plan
from implement_loop.model import Phase
from implement_loop.state import RunStore
from implement_loop.workspace import Workspace
from test_engine import EPIC, EngineCase

ANSWER = {"items": [], "zones": ["docs/probe.txt"], "decisions": [], "notes": "planned"}
REFUSAL = {
    "type": "result", "subtype": "success", "is_error": True,
    "api_error_status": 429, "api_error_type": "rate_limit_error",
    "api_error": "usage_limit_reached", "result": "You have hit your limit", "total_cost_usd": 0,
}


class PlanRecoveryProbe(EngineCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_work(57, "docs/probe.txt", "planned")
        self.epic(57)
        root = self.repo.work
        self.engine = Engine(
            build_plan(self.gh, EPIC, root), RunStore.for_root(root, EPIC), self.gh,
            Workspace(root, EPIC, SHIP, base_dir=self.repo.loop_dir), FakeAgents(self.scripts),
            Config(repo="test/test"), repo_root=root, log=lambda _: None, sleep=lambda _: None,
        )
        self.adapter = live.LiveAgents(
            "claude", root / "probe-calls", self.engine.ws, sleep=lambda _: None,
        )
        self.adapter.meter = self.engine
        self.models: list[str] = []
        self.refuse_all = True

    def spawn(self, cmd: list[str], *_args: object) -> tuple[int, str, str]:
        model = cmd[cmd.index("--model") + 1]
        self.models.append(model)
        if self.refuse_all or len(self.models) == 1:
            return 1, json.dumps(REFUSAL), ""
        return 0, json.dumps({"type": "result", "is_error": False, "structured_output": ANSWER}), ""

    def revive(self, failed_version: str) -> None:
        store = self.engine.store
        store.change_budget(57, set_infra=self.engine.caps.infra_budget)
        state = store.issue(57)
        state.phase = Phase.FAILED
        state.fail = {"cause": "ceiling", "version": failed_version, "resumed": False}
        self.engine._maybe_revive(state)
        store.put(state)
        self.assertEqual(state.phase, Phase.PENDING)
        self.assertEqual(store.budget(57)["infra"], 0)

    def test_new_version_revives_once_and_refusals_leave_plan_resumable(self) -> None:
        self.assertGreater(tuple(map(int, VERSION.split("."))), (1, 2, 12))
        self.revive("1.2.12")
        store = self.engine.store
        with patch.object(self.adapter, "_spawn", side_effect=self.spawn):
            for _ in range(self.engine.caps.infra_budget + 1):
                self.engine._set_phase(57, Phase.PLAN)
                with self.assertRaises(AgentFailure) as refused:
                    self.adapter.call("claude", "plan", self.repo.work, "Plan this issue", issue=57)
                self.assertEqual(refused.exception.kind, "outage")
                self.engine._finished(57, refused.exception)
                state = store.issue(57)
                self.assertEqual(state.phase, Phase.WAITING)
                self.assertIsNotNone(state.wait)
                assert state.wait is not None
                self.assertEqual((state.wait["kind"], state.wait["resume"]), ("outage", "plan"))
                budget = store.budget(57)
                self.assertEqual((budget["infra"], budget["calls"], budget["pending"]), (0, 0, 0))
                self.engine._maybe_resume(state)
                self.assertEqual(state.phase, Phase.PLAN)
                store.put(state)
        state.phase = Phase.FAILED
        self.engine._maybe_revive(state)
        self.assertEqual(state.phase, Phase.FAILED, "the revival is granted only once")

    def test_original_ceiling_revives_and_primary_refusal_uses_fallback(self) -> None:
        self.revive("1.2.4")
        self.refuse_all = False
        self.engine._set_phase(57, Phase.PLAN)
        with patch.object(self.adapter, "_spawn", side_effect=self.spawn):
            answer = self.adapter.call("claude", "plan", self.repo.work, "Plan this issue", issue=57)
        self.assertEqual(answer, ANSWER)
        role = self.adapter.roles["plan"]
        self.assertEqual(self.models, [role.claude_model, role.claude_fallback_model])
        budget = self.engine.store.budget(57)
        self.assertEqual((budget["infra"], budget["calls"], budget["pending"]), (0, 1, 0))
