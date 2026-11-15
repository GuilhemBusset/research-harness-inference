"""Regression for the engine fault b3ce2cd831 (#275): an issue spent its ceiling of 30 calls on a
head that never failed verification. Its run log shows two causes, both in the examine step.

The examiner returned one probe file whose criterion named several items. The engine compared
that string with item ids, so no behavioural item ever counted as probed: it asked the examiner
again twice, then the arbiter, at every examination. And the examiner answered `unmet` on an owner
item while quoting what the owner still owes; the engine sent that to the builder as a failed
round, and the builder had nothing to change.

The engine runs are real (git, the verifier, squash merges into a bare remote); the agents and
GitHub are the fakes, and the examiner answers as that issue's examiner did."""

import pytest

from helpers import SHIP, TempRepo, issue_body
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

EPIC, ISSUE = 1, 2
OWNERS = "Runs under a second on a 2020 laptop."   # D2, an owner item; its agent part is the fake plan's
PROBE = "checks/probe_2.sh"
Phase = L.model.Phase


class Examiner(L.agents.FakeAgents):
    """The fake agents, with an examiner whose default answer a test adjusts. What each
    examination was asked is kept."""

    def __init__(self, scripts, answer=None):
        super().__init__(scripts)
        self.answer = answer
        self.asked: list[list[str]] = []

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        self.asked.append(list(req.feedback))
        result = super().examine(vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since)
        return self.answer(result, req, since) if self.answer else result


class Loop:
    """A throwaway repository and one issue: D1 and A1 are proven by a test, D2 is the owner's."""

    def __init__(self):
        self.repo = TempRepo()
        self.fake = L.github.FakeGitHub(on_merge=self.repo.merge)
        body = issue_body("feat/issue-2", ["`docs/a.txt` contains `alpha`.", OWNERS], ["The test passes on main."])
        self.fake.add_issue(ISSUE, "Issue 2", body, labels=("size:M",))
        self.fake.add_issue(EPIC, "Epic", "An epic.\n", subs=(ISSUE,))

    def run(self, answer=None, **script):
        """One engine run; the issue's state after it."""
        self.agents = Examiner({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, owner_items=("D2",), **script)},
                               answer)
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)
        L.engine.Engine(
            L.graph.build_plan(self.fake, EPIC, self.repo.work), self.store, self.fake,
            L.workspace.Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir), self.agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None,
            policy={"lacks": ["hardware:laptop"]}).run()
        return self.store.issue(ISSUE)

    def events(self, kind):
        return [e for e in self.store.events() if e["kind"] == kind]

    def calls(self, role):
        return [c for c in self.agents.calls if c[0] == role]


@pytest.fixture
def loop():
    run = Loop()
    yield run
    run.repo.cleanup()


def test_one_probe_whose_criterion_names_several_items_covers_each_of_them(loop):
    def one_file_for_every_item(result, req, since):
        # the first examination names every item and says more; the later ones name the tested items
        criterion = "D1, A1" if since else "D1, A1; D2 agent portion"
        result.probes = [L.agents.Probe(PROBE, criterion, f"bash {PROBE}",
                                        content="#!/usr/bin/env bash\nset -e\ntest -s docs/a.txt\n")]
        return result

    st = loop.run(one_file_for_every_item)

    assert st.phase == Phase.DELIVERED, loop.events("round_failed")
    assert loop.events("examination_incomplete") == [], "D1 and A1 each have a probe: nothing is owed"
    assert loop.calls("arbitrate") == [], "no waiver is asked for items that are probed"
    assert [e["probes"] for e in loop.events("tests_locked") if e["probes"]] == [[PROBE]], "adopted once"
    assert len(loop.calls("examine")) == 2, "the examination, and the one of the head that adopted the probe"
    assert len(loop.calls("build")) == 1


def test_an_unmet_verdict_about_the_owners_evidence_is_asked_again_of_the_examiner(loop):
    def unmet_for_what_the_owner_owes(result, req, since):
        if not any("D2" in f for f in req.feedback):   # until the engine says what D2's verdict is about
            result.verdicts = [{"item": "D2", "verdict": "unmet", "quote": "Runs under a second",
                                "note": "the agents' part is complete; the owner has not timed it"}]
        return result

    st = loop.run(unmet_for_what_the_owner_owes)

    assert loop.events("round_failed") == [], "what the owner owes is not a round of the builder"
    assert len(loop.calls("build")) == 1
    assert [e["missing"] for e in loop.events("examination_incomplete")] == [["D2"]]
    assert loop.events("examined")[0]["unmet"] == []
    asked_again = loop.agents.asked[1]
    assert any("D2" in f and "time it on this host" in f for f in asked_again), "the examiner is given the agent part"
    assert st.verdicts["D2"]["verdict"] == "met"
    assert st.phase == Phase.DELIVERED


def test_an_unmet_verdict_that_quotes_the_agent_part_still_sends_the_builder_back(loop):
    st = loop.run(unmet_review=1)   # the fake examiner quotes the plan's agent part of D2

    assert loop.events("examined")[0]["unmet"] == ["D2"]
    assert any("D2" in p for e in loop.events("round_failed") for p in e["problems"])
    assert len(loop.calls("build")) == 2
    assert st.verdicts["D2"]["verdict"] == "met"
    assert st.phase == Phase.DELIVERED


@pytest.mark.parametrize(("criterion", "named"), [
    ("D1", {"D1"}),
    ("D1, D2, D3, A1, A2; A3 agent portion", {"D1", "D2", "D3", "A1", "A2", "A3"}),
    ("D1 and A1: the file's content", {"D1", "A1"}),
    ("D1: read as D2 reads it", {"D1"}),          # only the list it leads with
    ("D1, D9", {"D1"}),                           # the plan has no D9
    ("the file's content, as D1 asks", set()),
    (None, set()),
])
def test_a_criterion_names_the_items_it_leads_with(criterion, named):
    assert L.engine.criterion_items(criterion, {"D1", "D2", "D3", "A1", "A2", "A3"}) == named
