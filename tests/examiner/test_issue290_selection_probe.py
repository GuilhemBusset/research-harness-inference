"""Independent checks of the repair for issue 290."""

import sys
from pathlib import Path

import pytest

from helpers import SHIP, TempRepo, git, issue_body
from test_deploying import L

SUITE = "tests/test_prior.py"
ADDED = f"{SUITE}::TestBranch::test_extra"


class SelectionAgents(L.agents.FakeAgents):
    def __init__(self, added):
        super().__init__({2: L.agents.Script(files={"docs/result.txt": "ready"}, findings=1)})
        self.added = added

    def plan(self, vendor, req, worktree):
        result = super().plan(vendor, req, worktree)
        if self.added:
            result.zones.append(SUITE)
        return result

    def build(self, vendor, req, plan, worktree, feedback):
        first = self._built.get(2, 0) == 0
        result = super().build(vendor, req, plan, worktree, feedback)
        if first and self.added:
            with (worktree / SUITE).open("a") as target:
                target.write("\nclass TestBranch:\n    def test_extra(self):\n        assert 6 // 2 == 3\n")
        if any("no command selects" in message for message in req.feedback):
            result.dispositions = {ADDED: "excluded: this test witnesses new-test accounting"}
        result.commands = [command for command in result.commands if command.criterion != "A1"]
        result.commands.append(L.model.CheckSpec(
            "A1", command=f"{sys.executable} -m pytest -q -p no:cacheprovider {SUITE}::test_selected"))
        return result


@pytest.mark.parametrize("added", [False, True])
def test_existing_suite_is_exempt_but_branch_additions_need_selection(added):
    repo = TempRepo()
    try:
        suite = repo.merger / SUITE
        suite.parent.mkdir()
        suite.write_text(
            "import pytest\n\n"
            "@pytest.mark.parametrize('value', [2, 4])\n"
            "def test_selected(value):\n    assert value % 2 == 0\n\n"
            "class TestPrior:\n    def test_unselected(self):\n        assert 3 * 3 == 9\n")
        git("add", SUITE, cwd=repo.merger)
        git("commit", "-qm", "Seed the prior suite", cwd=repo.merger)
        git("push", "-q", "origin", "HEAD:main", cwd=repo.merger)
        gh = L.github.FakeGitHub(on_merge=repo.merge)
        gh.add_issue(1, "Epic", "An epic.", subs=(2,))
        gh.add_issue(2, "Selection", issue_body(
            "feat/issue-2", ["`docs/result.txt` contains ready."], ["The selected test passes."]),
            labels=("size:M",))
        store = L.state.RunStore.for_root(repo.work, 1)
        L.engine.Engine(
            L.graph.build_plan(gh, 1, repo.work), store, gh,
            L.workspace.Workspace(repo.work, 1, SHIP, base_dir=repo.loop_dir), SelectionAgents(added),
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=repo.work, log=lambda message: None, sleep=lambda seconds: None,
            policy={"lacks": []}).run()
        state = store.issue(2)
        problems = [problem for event in store.events() if event["kind"] == "verified"
                    for problem in event["problems"] if "no command selects" in problem]
        assert state.phase == L.model.Phase.DONE
        assert SUITE in state.locked
        assert bool(problems) == added
        assert all(ADDED in problem and "TestPrior" not in problem for problem in problems)
        assert any("test_selected[" in node for nodes in state.manifest.values() for node in nodes)
    finally:
        repo.cleanup()


def test_repair_version_is_newer_and_documented():
    engine = Path(__file__).resolve().parents[2] / "tools/implementation-loop"
    version = L.engine.VERSION
    assert tuple(map(int, version.split("."))) > (1, 2, 9)
    notes = (engine / "UPSTREAM.md").read_text()
    assert f"| Engine version here | {version} |" in notes
    assert f"### {version}:" in notes
