"""An issue another root's run delivered stays open for the owner's items; a later root whose work
it is takes that delivery instead of building it again (#122, 2026-11-19)."""

import unittest

from helpers import SHIP, TempRepo, issue_body
from implement_loop.agents import FakeAgents, Script
from implement_loop.config import Caps, Config
from implement_loop.engine import Engine
from implement_loop.github import FakeGitHub
from implement_loop.graph import build_plan
from implement_loop.model import IssueState, Phase
from implement_loop.state import RunStore
from implement_loop.workspace import Workspace

OWNER_ITEM = {"item": "A3", "kind": "capability", "detail": "needs aws:deployed-environment"}


class AdoptedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.gh = FakeGitHub(on_merge=self.repo.merge)
        self.gh.add_issue(2, "Issue 2", issue_body("feat/issue-2", ["`docs/issue-2.txt` contains `two`."],
                                                   ["The check passes."]), labels=("size:M", "loop:delivered"))
        self.gh.add_issue(4, "Epic 4", "Test epic.\n", subs=(2,))
        self.agents = FakeAgents({2: Script(files={"docs/issue-2.txt": "two"})})

    def engine(self, store: RunStore) -> Engine:
        return Engine(build_plan(self.gh, 4, self.repo.work), store, self.gh,
                      Workspace(self.repo.work, 4, SHIP, base_dir=self.repo.loop_dir / "run-4"),
                      self.agents, Config(repo="test/test", caps=Caps(ci_poll_s=0)),
                      repo_root=self.repo.work, log=lambda _: None, sleep=lambda _: None)

    def test_a_delivery_of_another_root_is_taken_once_and_nothing_is_built_again(self):
        other = RunStore.for_root(self.repo.work, 2)
        other.put(IssueState(number=2, phase=Phase.DELIVERED, pr=7, obligations=[OWNER_ITEM]))
        store = RunStore.for_root(self.repo.work, 4)
        store.put(IssueState(number=2, phase=Phase.FAILED, pr=9))
        engine = self.engine(store)

        engine.run()

        st = store.issue(2)
        self.assertEqual((st.phase, st.pr, st.obligations), (Phase.DELIVERED, 7, [OWNER_ITEM]))
        self.assertEqual([c for c in self.agents.calls if c[0] in ("plan", "build")], [])
        adopted = [e for e in store.events() if e["kind"] == "delivery_adopted"]
        self.assertEqual([(e["issue"], e["root"], e["previous"]) for e in adopted], [(2, 2, "failed")])
        st.phase = Phase.BUILD   # a round this run starts later is its own
        store.put(st)
        engine._adopt_deliveries()
        self.assertEqual(store.issue(2).phase, Phase.BUILD)

    def test_without_a_delivery_elsewhere_the_issue_is_built_here(self):
        other = RunStore.for_root(self.repo.work, 2)
        other.put(IssueState(number=2, phase=Phase.BUILD))
        store = RunStore.for_root(self.repo.work, 4)

        self.engine(store).run()

        self.assertEqual(store.issue(2).phase, Phase.DONE)
        self.assertEqual([e for e in store.events() if e["kind"] == "delivery_adopted"], [])


if __name__ == "__main__":
    unittest.main()
