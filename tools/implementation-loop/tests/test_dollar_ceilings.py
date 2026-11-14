"""Dollar ceilings (ADR 0001, decision 8): each Claude call carries `--max-budget-usd` from its
role's setting, a call that stops there is not tried again, what every call cost is charged to
its issue, and an issue that has spent its dollar ceiling fails like one that spent its calls."""

import argparse
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import load_fixture
from implement_loop import cli, config, live
from implement_loop.agents import AgentFailure, CeilingReached, FakeAgents, IssueRequest
from implement_loop.issue_parser import parse
from implement_loop.model import Issue, Phase
from test_live import ANSWERS, FAKE, PLAN, StubWorkspace
from test_review_fixes import FixCase

# A claude that stops at its budget: no answer, an error result that says so, and what it spent.
OVER_BUDGET = r'''#!{python}
import json, os, sys
sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:]}}) + "\n")
print(json.dumps({{"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 8.02,
                   "errors": ["Reached maximum budget ($8)"], "usage": {{"input_tokens": 9, "output_tokens": 9}}}}))
sys.exit(1)
'''


class SpendingMeter:
    def __init__(self):
        self.costs, self.outcomes = [], []

    def before(self, issue, role, vendor):
        pass

    def after(self, issue, role, vendor, outcome):
        self.outcomes.append(outcome)

    def spent(self, issue, role, vendor, cost_usd):
        self.costs.append((issue, role, vendor, cost_usd))


class CallBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-usd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bins = self.tmp / "bin"
        self.bins.mkdir()
        for name in ("claude", "codex"):
            self.install(name, FAKE)
        self.log, self.answer = self.tmp / "calls.jsonl", self.tmp / "answer.json"
        patcher = mock.patch.dict(os.environ, {"FAKE_LOG": str(self.log), "FAKE_ANSWER": str(self.answer)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps: list[float] = []
        self.agents = live.LiveAgents("claude", self.tmp / "run", StubWorkspace(self.tmp),
                                      claude_bin=str(self.bins / "claude"), codex_bin=str(self.bins / "codex"),
                                      sleep=self.sleeps.append)
        self.agents.meter = self.meter = SpendingMeter()
        issue = Issue.from_api(load_fixture()["issues"]["28"])
        self.req = IssueRequest(issue=issue, spec=parse(issue), context="ctx")

    def install(self, name: str, script: str) -> None:
        path = self.bins / name
        path.write_text(script.format(python=sys.executable))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def argv(self, vendor: str, role: str) -> list[str]:
        self.answer.write_text(json.dumps(ANSWERS[role]))
        if role == "build":
            self.agents.build(vendor, self.req, PLAN, self.tmp, [])
        elif role == "plan":
            self.agents.plan(vendor, self.req, None)
        elif role == "triage":
            self.agents.triage(vendor, self.req, PLAN)
        else:
            self.agents.explore(vendor, "a question", "the briefing")
        return json.loads(self.log.read_text().splitlines()[-1])["argv"]

    def test_loop_toml_sets_the_provisional_budgets(self):
        roles = live.load_roles()
        self.assertEqual({r: s.max_budget_usd for r, s in roles.items() if s.max_budget_usd},
                         {"explore": 2, "plan": 8, "build": 25})
        self.assertEqual({r: s.max_budget_usd for r, s in live.DEFAULT_ROLES.items() if s.max_budget_usd},
                         {"explore": 2, "plan": 8, "build": 25}, "and the code's defaults say the same")

    def test_each_claude_call_carries_its_roles_budget(self):
        for role, budget in (("explore", "2"), ("plan", "8"), ("build", "25")):
            with self.subTest(role=role):
                argv = self.argv("claude", role)
                self.assertEqual(argv[argv.index("--max-budget-usd") + 1], budget)

    def test_a_role_without_a_budget_and_every_codex_call_carry_none(self):
        self.assertNotIn("--max-budget-usd", self.argv("claude", "triage"))
        for role in ("explore", "plan", "build"):
            self.assertNotIn("--max-budget-usd", self.argv("codex", role), "Codex reports tokens, not dollars")

    def test_the_setting_is_per_role(self):
        self.agents.roles["build"].max_budget_usd = 3.5
        argv = self.argv("claude", "build")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "3.5")
        self.agents.roles["build"].max_budget_usd = None
        self.assertNotIn("--max-budget-usd", self.argv("claude", "build"))

    def test_what_a_call_cost_is_reported_to_the_meter(self):
        self.argv("claude", "build")
        self.assertEqual(self.meter.costs, [(28, "build", "claude", 0.01)])
        self.argv("codex", "triage")
        self.assertEqual(len(self.meter.costs), 1, "Codex reports no dollars")

    def test_a_call_that_stops_at_its_budget_is_charged_and_not_tried_again(self):
        self.install("claude", OVER_BUDGET)
        with self.assertRaises(AgentFailure) as raised:
            self.agents.plan("claude", self.req, None)
        self.assertEqual(len(self.log.read_text().splitlines()), 1, "one attempt: the same call would stop at the same place")
        self.assertEqual(self.sleeps, [], "no backoff, no retry")
        self.assertEqual(raised.exception.kind, "crash")
        self.assertIn("stopped at its budget of $8 for one call", str(raised.exception))
        self.assertEqual(self.meter.costs, [(28, "plan", "claude", 8.02)], "what it spent is charged all the same")
        self.assertEqual(self.meter.outcomes, ["crash"])
        record = json.loads((self.tmp / "run" / "agents.jsonl").read_text().splitlines()[-1])
        self.assertEqual((record["cost_usd"], record["over_budget"], record["ok"]), (8.02, True, False))

    def test_a_meter_that_takes_no_costs_is_left_alone(self):
        class Plain:
            def before(self, *a):
                pass

            def after(self, *a):
                pass
        self.agents.meter = Plain()   # a test double
        self.argv("claude", "build")


class IssueCeilingTests(FixCase):
    def test_the_ceilings_by_size(self):
        self.assertEqual(config.USD_CEILINGS, {"S": 40.0, "M": 70.0, "L": 120.0})
        self.assertEqual([config.usd_ceiling(size, False) for size in ("S", "M", "L", "unknown")], [40, 70, 120, 70])
        self.assertEqual(config.usd_ceiling("S", True), 60, "factory tooling gets one and a half, like its calls")

    def test_costs_add_up_and_the_ceiling_refuses_the_next_call(self):
        self.add_work(2, "docs/a.txt", "alpha", size="S")
        self.epic(2)
        engine = self.make_engine()
        engine.spent(2, "build", "claude", 24.5)
        engine.before(2, "build", "claude")   # under the ceiling: allowed
        engine.after(2, "build", "claude", "answered")
        engine.spent(2, "build", "claude", 15.5)
        self.assertEqual(engine.store.budget(2)["usd"], 40.0)
        with self.assertRaises(CeilingReached) as raised:
            engine.before(2, "examine", "codex")
        self.assertEqual(raised.exception.scope, "issue")
        self.assertIn("#2's ceiling of $40.00 is spent ($40.00 so far)", str(raised.exception))
        costs = [e for e in engine.store.events() if e["kind"] == "agent_cost"]
        self.assertEqual([(e["cost_usd"], e["usd"]) for e in costs], [(24.5, 24.5), (15.5, 40.0)])

    def test_only_a_known_issue_and_a_real_cost_are_charged(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        engine = self.make_engine()
        for issue, cost in ((None, 5.0), (99, 5.0), (2, 0), (2, -1.0), (2, None)):
            engine.spent(issue, "explore", "claude", cost)   # a vendor's odd answer
        self.assertNotIn("usd", engine.store.budget(2))
        self.assertNotIn("usd", engine.store.budget(99))

    def test_a_stale_state_copy_does_not_lose_a_charge(self):
        self.add_work(2, "docs/a.txt", "alpha")
        self.epic(2)
        engine = self.make_engine()
        stale = engine.store.issue(2)
        engine.spent(2, "build", "claude", 3.25)
        engine.store.put(stale)
        self.assertEqual(engine.store.budget(2)["usd"], 3.25)

    def test_an_issue_that_spends_its_dollars_fails_like_one_that_spends_its_calls(self):
        class Costly(FakeAgents):
            """Every build is answered, wrong, and costs fifteen dollars."""

            def build(self, vendor, req, plan, worktree, feedback):
                result = super().build(vendor, req, plan, worktree, feedback)
                getattr(self.meter, "spent")(req.issue.number, "build", vendor, 15.0)   # noqa: B009 - not on the protocol
                return result
        self.add_work(2, "docs/a.txt", "alpha", size="S", wrong_attempts=99)
        self.epic(2)
        agents = Costly(self.scripts)
        self.run_engine(agents=agents)
        st = self.store.issue(2)
        self.assertEqual((st.phase, (st.fail or {}).get("cause")), (Phase.FAILED, "ceiling"))
        self.assertIn("ceiling of $40.00 is spent", (st.fail or {}).get("detail", ""))
        builds = [c for c in agents.calls if c[0] == "build"]
        self.assertEqual(len(builds), 3, "the call after $45 was refused before it started")
        self.assertEqual(self.store.budget(2)["usd"], 45.0)
        self.assertLess(self.store.budget(2)["calls"], 20, "well under its call ceiling: the dollars stopped it")
        self.assertEqual(self.gh.prs, {})
        self.assertTrue(self.events("failed", 2))

    def test_a_retry_grants_fresh_dollars_and_an_owners_edit_resets_them(self):
        self.add_work(2, "docs/a.txt", "alpha", size="S")
        self.epic(2)
        engine = self.make_engine()
        engine.spent(2, "build", "claude", 41.0)
        st = engine.store.issue(2)
        st.phase, st.fail = Phase.FAILED, {"cause": "ceiling", "detail": "spent"}
        engine.store.put(st)
        out = io.StringIO()
        with mock.patch.object(cli, "repo_root", return_value=self.repo.work), redirect_stdout(out):
            cli.cmd_retry(argparse.Namespace(issue=1, items=[]))
            cli.cmd_status(argparse.Namespace(issue=1))
        self.assertIn("#2 gets a fresh ceiling", out.getvalue())
        self.assertIn("$41.00", out.getvalue(), "the status shows what the issue cost")
        engine = self.make_engine()
        self.assertEqual(engine.store.budget(2)["usd_bonus"], 41.0)
        self.assertEqual(engine._usd_ceiling(2), 81.0, "forty more from here")
        engine.before(2, "build", "claude")
        engine.after(2, "build", "claude", "answered")
        st = engine.store.issue(2)
        engine._owner_edit(st)
        b = engine.store.budget(2)
        self.assertEqual((b["usd"], b["usd_bonus"]), (0, 0), "a changed requirement starts from nothing")

    def test_a_new_engine_version_revives_a_failed_issue_with_fresh_dollars(self):
        self.add_work(2, "docs/a.txt", "alpha", size="S")
        self.epic(2)
        engine = self.make_engine()
        engine.spent(2, "build", "claude", 44.0)
        st = engine.store.issue(2)
        st.phase, st.fail = Phase.FAILED, {"cause": "ceiling", "detail": "spent", "version": "0.0.1"}
        engine._maybe_revive(st)
        self.assertEqual(engine.store.budget(2)["usd_bonus"], 44.0)
        engine.before(2, "build", "claude")


if __name__ == "__main__":
    unittest.main()
