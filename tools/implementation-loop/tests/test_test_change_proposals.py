"""Regression for the engine fault 10fcb07f92 (#274): an issue ended `no_progress` because the
examiner could never read the change its builder proposed to a locked test.

The engine reverts a builder's edit to a locked test before the other vendor decides the
`test_change` request, so the decider's throwaway copy held the locked version, and the proposed
file reached it only as text inside the requests. The live adapter cuts that text to fit its
prompt: the replacement stopped mid-function, the requests listed after it were gone, and the
examiner refused the same request in five builds.

The engine runs are real (git, the verifier, squash merges into a bare remote); the agents and
GitHub are the fakes. The decider keeps what it was given, and the prompt the live adapter writes
for those requests."""

import importlib
import json
from types import SimpleNamespace

import pytest

from helpers import SHIP, TempRepo, issue_body
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

EPIC, ISSUE = 1, 2
LOCKED = "checks/issue_2.sh"
BESIDE = "docs/note.txt"
END = "# the last line of the proposal\n"
Phase = L.model.Phase
live = importlib.import_module(L.engine.__name__.rsplit(".", 1)[0] + ".live")


class Agents(L.agents.FakeAgents):
    """The fake agents. The second build, which follows the examination that locked the issue's
    test, changes that test with `change` and asks for it with a `test_change` request, followed
    by a second request. The decider records what it holds and answers `approve`."""

    def __init__(self, scripts, change, approve=True):
        super().__init__(scripts)
        self.change, self.approve = change, approve
        self.decided: list[SimpleNamespace] = []

    def build(self, vendor, req, plan, worktree, feedback):
        second = self._built.get(ISSUE, 0) == 1
        result = super().build(vendor, req, plan, worktree, feedback)
        if second:
            self.change(worktree / LOCKED)
            result.requests = [L.agents.Request("test_change", "the locked test is wrong for the item", paths=[LOCKED]),
                               L.agents.Request("paths", "a note beside the test", paths=[BESIDE])]
        return result

    def decide(self, vendor, req, worktree, requests):
        sent = []
        adapter = SimpleNamespace(_issue_vars=live.LiveAgents._issue_vars,
                                  call=lambda vendor, role, cwd, prompt, issue, escalate=False: sent.append(prompt) or {"decisions": []})
        live.LiveAgents.decide(adapter, vendor, req, worktree, requests)
        mine = worktree / LOCKED
        self.decided.append(SimpleNamespace(
            requests=json.loads(json.dumps(requests)), prompt=sent[0],
            copy=mine.read_text() if mine.is_file() else None,
            builders=(self.builders_worktree / LOCKED).read_text()))
        return [L.agents.Decision(r["id"], self.approve, "decided by the test") for r in requests]


class Loop:
    """A throwaway repository and one issue, whose first examination leaves a finding: the
    builder builds twice, and its second build finds the issue's test locked."""

    def __init__(self):
        self.repo = TempRepo()
        self.fake = L.github.FakeGitHub(on_merge=self.repo.merge)
        body = issue_body("feat/issue-2", ["`docs/a.txt` contains `alpha`."], ["The test passes on main."])
        self.fake.add_issue(ISSUE, "Issue 2", body, labels=("size:M",))
        self.fake.add_issue(EPIC, "Epic", "An epic.\n", subs=(ISSUE,))

    def run(self, change, approve=True):
        """One engine run; what the decider held at its one call."""
        self.agents = Agents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, findings=1)}, change, approve)
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)
        workspace = L.workspace.Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir)
        self.agents.builders_worktree = workspace.dir / f"issue-{ISSUE}"
        L.engine.Engine(
            L.graph.build_plan(self.fake, EPIC, self.repo.work), self.store, self.fake, workspace, self.agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None).run()
        (held,) = self.agents.decided
        return held


@pytest.fixture
def loop():
    run = Loop()
    yield run
    run.repo.cleanup()


def test_the_decider_holds_the_whole_proposal_and_every_request(loop):
    proposals = []

    def a_long_replacement(test):
        locked = test.read_text()
        # larger than any prompt's share for the requests: 1.2.4 gave the decider its first 20,000 characters
        proposals.append(locked + "".join(f"# case {i}: one more line of a long test\n" for i in range(3000)) + END)
        test.write_text(proposals[0])

    held = loop.run(a_long_replacement)
    (proposal,) = proposals
    locked = proposal[:proposal.index("# case 0")]

    assert held.copy == proposal, "the decider reads and runs the proposal in its working directory, to its last line"
    assert held.builders == locked, "the builder's branch keeps the locked test until the request is approved"
    change, beside = held.requests
    assert (change["kind"], change["paths"], change["proposed"]) == ("test_change", [LOCKED], {LOCKED: "replaced"})
    assert END.strip() not in json.dumps(held.requests), "the request names the file; its text is in the copy"
    # what the live adapter makes of these requests: all of them, and whole
    block = held.prompt.split("<requests>")[1].split("</requests>")[0]
    assert [r["id"] for r in json.loads(block)] == [change["id"], beside["id"]]
    # approved, the complete proposal is what lands
    assert loop.store.issue(ISSUE).phase == Phase.DONE, [e for e in loop.store.events() if e["kind"] == "round_failed"]
    assert loop.repo.main_file(LOCKED) == proposal.strip()   # `main_file` strips what git prints


def test_a_locked_test_the_builder_deletes_is_absent_from_the_deciders_copy(loop):
    held = loop.run(lambda test: test.unlink(), approve=False)

    assert held.copy is None, "the decider sees the tree the builder proposes: without the test"
    assert held.requests[0]["proposed"] == {LOCKED: "deleted"}
    assert held.builders, "the builder's branch keeps the locked test"
    assert loop.store.issue(ISSUE).phase == Phase.DONE
    assert loop.repo.main_file(LOCKED) == held.builders.strip(), "refused: the locked test lands as it was"
