"""Regression for the engine fault e5e7b9c1d5 (#246): the build step asked for an issue's worktree
(`Engine._step_build`, `Workspace.issue_worktree`, `_issue_worktree`), git refused the
`worktree add`, and the `GitError` of `Workspace.git` crashed the step. git refuses that command
when it still registers a worktree whose directory was deleted, which the engine now repairs
(`git worktree prune`, then the add again), and when another worktree that exists has the branch
checked out, for which the issue now waits.

The engine runs are real (git, the verifier, squash merges into a bare remote); the agents and
GitHub are the fakes."""

import shutil

import pytest

from helpers import SHIP, TempRepo, git, issue_body
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

EPIC, ISSUE, BRANCH = 1, 2, "feat/issue-2"
Phase = L.model.Phase


class Loop:
    """A throwaway repository, one issue that writes a file, the fakes, and engine runs on one store."""

    def __init__(self):
        self.repo = TempRepo()
        self.fake = L.github.FakeGitHub(on_merge=self.repo.merge)
        body = issue_body(BRANCH, ["`docs/a.txt` contains `alpha`."], ["The test passes on main."])
        self.fake.add_issue(ISSUE, "Issue 2", body, labels=("size:M",))
        self.fake.add_issue(EPIC, "Epic", "An epic.\n", subs=(ISSUE,))
        self.agents = L.agents.FakeAgents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"})})
        self.ws = L.workspace.Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.tmp / "loop")

    def run(self):
        """One engine run; the issue's state after it."""
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)
        L.engine.Engine(
            L.graph.build_plan(self.fake, EPIC, self.repo.work), self.store, self.fake, self.ws, self.agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None, policy={"lacks": []}).run()
        return self.store.issue(ISSUE)

    def events(self, kind):
        return [e for e in self.store.events() if e["kind"] == kind]


@pytest.fixture
def loop():
    run = Loop()
    yield run
    run.repo.cleanup()


def test_a_deleted_worktree_git_still_registers_is_added_again_with_its_branch(loop):
    wt = loop.ws.issue_worktree(ISSUE, BRANCH)   # what an earlier run left: the branch, and a commit on it
    git("commit", "-q", "--allow-empty", "-m", "work of an earlier run", cwd=wt)
    kept = git("rev-parse", "HEAD", cwd=wt)
    shutil.rmtree(wt)
    with pytest.raises(RuntimeError):   # the fault's command: git still registers the worktree and refuses
        git("worktree", "add", "--quiet", str(wt), BRANCH, cwd=loop.repo.work)

    st = loop.run()

    assert loop.events("step_crashed") == [], "a worktree deleted from under git is not an engine fault"
    assert st.phase == Phase.DONE
    assert loop.repo.main_file("docs/a.txt") == "alpha"
    built = loop.events("built")[0]["sha"]
    git("merge-base", "--is-ancestor", kept, built, cwd=loop.repo.work)   # raises unless the build is on that commit


def test_a_new_branch_takes_the_place_of_a_deleted_worktree(loop):
    # the registration is of another branch (an earlier round's), and the issue's branch does not exist yet
    old = loop.ws.issue_worktree(ISSUE, "feat/issue-2-earlier")
    shutil.rmtree(old)

    wt = loop.ws.issue_worktree(ISSUE, BRANCH)

    assert wt == old
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == BRANCH
    assert git("rev-parse", "HEAD", cwd=wt) == git("rev-parse", "origin/main", cwd=loop.repo.work)


def test_a_branch_checked_out_in_another_worktree_makes_the_issue_wait(loop):
    elsewhere = loop.repo.tmp / "elsewhere"
    git("worktree", "add", "--quiet", "-b", BRANCH, str(elsewhere), "origin/main", cwd=loop.repo.work)
    (elsewhere / "notes.txt").write_text("work in progress\n")

    st = loop.run()

    assert loop.events("step_crashed") == [], "a branch held elsewhere is not an engine fault"
    assert (st.phase, st.wait["kind"], st.wait["resume"]) == (Phase.WAITING, L.model.WAIT_CLAIM, "build")
    assert [c for c in loop.agents.calls if c[0] == "build"] == [], "nothing is built on a branch held elsewhere"
    assert (elsewhere / "notes.txt").read_text() == "work in progress\n", "the other worktree is left as it is"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=elsewhere) == BRANCH

    git("worktree", "remove", "--force", str(elsewhere), cwd=loop.repo.work)   # the branch is free again
    assert loop.run().phase == Phase.DONE, "the next run resumes the build"
    assert loop.repo.main_file("docs/a.txt") == "alpha"


def test_any_other_refusal_is_still_a_git_error(loop):
    stray = loop.repo.tmp / "loop" / f"issue-{ISSUE}" / "stray.txt"   # the place is taken by a directory git does not know
    stray.parent.mkdir(parents=True)
    stray.write_text("not a worktree\n")
    with pytest.raises(L.workspace.GitError) as refusal:
        loop.ws.issue_worktree(ISSUE, BRANCH)
    assert type(refusal.value) is L.workspace.GitError
    assert stray.read_text() == "not a worktree\n"
