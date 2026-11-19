"""A root settles once its own work has: an issue it recorded once (a prerequisite pulled in from
another epic) and that another run has since closed must not keep it running (#2, 2026-11-19)."""

import unittest

from helpers import SHIP, TempRepo, issue_body
from implement_loop.agents import FakeAgents, Script
from implement_loop.config import Caps, Config
from implement_loop.engine import Engine
from implement_loop.github import FakeGitHub
from implement_loop.graph import build_plan
from implement_loop.model import IssueState, Phase
from implement_loop.state import RunStore
from implement_loop.supervisor import Supervisor
from implement_loop.workspace import Workspace


class ClosedElsewhereTests(unittest.TestCase):
    def test_a_recorded_issue_closed_by_another_run_is_done_and_the_root_settles(self):
        repo = TempRepo()
        self.addCleanup(repo.cleanup)
        files = {"docs/issue-2.txt": "content for issue 2"}
        gh = FakeGitHub(on_merge=repo.merge)
        gh.add_issue(2, "Issue 2", issue_body("feat/issue-2", ["`docs/issue-2.txt` contains `content for issue 2`."],
                                              ["The check passes."]), labels=("size:M",))
        gh.add_issue(1, "Epic 1", "Test epic.\n", subs=(2,))
        gh.add_issue(3, "Closed by another run", state="closed")
        gh.add_issue(4, "Still open elsewhere")
        store = RunStore.for_root(repo.work, 1)
        store.put(IssueState(number=3, phase=Phase.WAITING, wait={"kind": "claim", "detail": "another run held it"}))
        store.put(IssueState(number=4, phase=Phase.BUILD))
        engine = Engine(build_plan(gh, 1, repo.work), store, gh,
                        Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir / "run-1"),
                        FakeAgents({2: Script(files=files)}), Config(repo="test/test", caps=Caps(ci_poll_s=0)),
                        repo_root=repo.work, log=lambda _: None, sleep=lambda _: None)

        engine.run()

        self.assertEqual(store.issue(2).phase, Phase.DONE)
        self.assertEqual((store.issue(3).phase, store.issue(3).wait), (Phase.DONE, None))
        self.assertEqual(store.issue(4).phase, Phase.BUILD, "an issue still open on GitHub is left as it was")
        self.assertEqual([e["issue"] for e in store.events() if e["kind"] == "closed_elsewhere"], [3])
        supervisor = Supervisor(repo.work, 1, "claude", log=lambda _: None)
        self.assertFalse(supervisor._all_settled(store), "#4 still open keeps it running")
        gh.issues[4]["state"] = "closed"
        engine._settle_closed_elsewhere()
        self.assertTrue(supervisor._all_settled(store))


if __name__ == "__main__":
    unittest.main()
