"""Regression for the engine fault 75fe7a2823 (#290): #51 ended `no_progress`. One of its commands
ran a single test of an existing suite by node id; at the first examination that file locked, and
from then on every other test main defines in it was charged to the issue as "tests in locked
files that no command selects", round after round and through a replan.

The engine runs are real (git, the verifier, squash merges into a bare remote); the agents and
GitHub are the fakes."""

import sys

import pytest

from helpers import SHIP, TempRepo, git, issue_body
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

EPIC, ISSUE = 1, 2
SUITE = "tests/test_existing_suite.py"
Phase = L.model.Phase


class Builder(L.agents.FakeAgents):
    """The fake agents, whose builder proves A1 by one test of main's suite, by node id. With
    `added`, its first build also adds a test to that file, and it disposes of that test once its
    feedback reports it unselected."""

    def __init__(self, scripts, added: bool):
        super().__init__(scripts)
        self.added = added

    def plan(self, vendor, req, worktree):
        result = super().plan(vendor, req, worktree)
        result.zones = sorted(set(result.zones) | ({SUITE} if self.added else set()))
        return result

    def build(self, vendor, req, plan, worktree, feedback):
        first = self._built.get(req.issue.number, 0) == 0
        result = super().build(vendor, req, plan, worktree, feedback)
        if self.added and first:
            with (worktree / SUITE).open("a") as f:
                f.write("def test_added():\n    assert 1 + 1 == 2\n")
        if self.added and any("no command selects" in f for f in req.feedback):
            result.dispositions = {f"{SUITE}::test_added": "excluded: the counter-case's own test"}
        result.commands = [c for c in result.commands if c.criterion != "A1"] + [
            L.model.CheckSpec("A1", command=f"{sys.executable} -m pytest -q -p no:cacheprovider {SUITE}::test_one")]
        return result


class Loop:
    """A throwaway repository whose `main` already has a test file of three tests, and one issue."""

    def __init__(self):
        self.repo = TempRepo()
        merger = self.repo.merger
        (merger / "tests").mkdir(exist_ok=True)
        (merger / SUITE).write_text("".join(f"def test_{name}():\n    assert 1 + 1 == 2\n\n"
                                            for name in ("one", "two", "three")))
        git("add", SUITE, cwd=merger)
        git("commit", "-q", "-m", "an existing suite", cwd=merger)
        git("push", "-q", "origin", "HEAD:main", cwd=merger)
        self.fake = L.github.FakeGitHub(on_merge=self.repo.merge)
        body = issue_body("feat/issue-2", ["`docs/a.txt` contains `alpha`."], ["The test passes on main."])
        self.fake.add_issue(ISSUE, "Issue 2", body, labels=("size:M",))
        self.fake.add_issue(EPIC, "Epic", "An epic.\n", subs=(ISSUE,))

    def run(self, added=False):
        """One engine run, with a first examination that leaves a finding; the issue's state after it."""
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)
        agents = Builder({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, findings=1)}, added)
        L.engine.Engine(
            L.graph.build_plan(self.fake, EPIC, self.repo.work), self.store, self.fake,
            L.workspace.Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir), agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None,
            policy={"lacks": ["hardware:laptop"]}).run()
        return self.store.issue(ISSUE)

    def events(self, kind):
        return [e for e in self.store.events() if e["kind"] == kind and e.get("issue") == ISSUE]

    def unselected(self):
        return [p for e in self.events("verified") for p in e["problems"] if "no command selects" in p]


@pytest.fixture
def loop():
    run = Loop()
    yield run
    run.repo.cleanup()


def test_the_tests_main_already_defines_in_a_locked_file_are_not_charged_to_the_issue(loop):
    st = loop.run()

    assert st.phase == Phase.DONE, loop.events("round_failed")
    assert SUITE in st.locked, "the command ran a test of the file, so it locked"
    assert loop.unselected() == []


def test_a_test_the_branch_adds_to_that_file_is_still_required(loop):
    st = loop.run(added=True)

    assert st.phase == Phase.DONE, loop.events("round_failed")
    reported = loop.unselected()
    assert reported and all("test_added" in p for p in reported), reported
    assert not [p for p in reported if "test_two" in p or "test_three" in p], reported
