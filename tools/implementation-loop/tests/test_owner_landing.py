"""A certified change to a workflow file is landed by the owner (ADR 0001, amendment 2; issue #17):
the engine pushes nothing of it, waits (`owner_landing`), opens the pull request once the owner
pushed the certified head, and leaves the merge to the owner.

The engine runs are real (git, the verifier, a bare remote). The agents and GitHub are fakes. The
test plays the owner: it runs the push command the engine's comment gives, in the repository the
engine runs in, and merges the pull request with a real squash in the remote, without the fake's
`merge_pr`, so every `merge_pr` in the fake's log is the engine's."""

import re
import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path

from helpers import SHIP, TempRepo, git, issue_body, present
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

EPIC, ISSUE = 1, 2
BRANCH = f"feat/issue-{ISSUE}"
WORKFLOW = ".github/workflows/ci.yml"
DEPLOY = "deploy/beta"
Phase = L.model.Phase
OWNER_LANDING = L.model.WAIT_OWNER_LANDING
MARKER = L.engine.OWNER_LANDING_MARKER.format("")[:-4]   # what every such comment starts with, whatever its head


class MainMovesFirst(L.agents.FakeAgents):
    """The fake agents; `during_examination` runs once, while the first examination is made."""

    during_examination = None

    def examine(self, vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since):
        todo, self.during_examination = self.during_examination, None
        if todo:
            todo()
        return super().examine(vendor, req, plan, worktree, base_sha, head_sha, evidence, memory, requests, since)


class Loop:
    """A repository whose `main` has the workflow file of CI, one issue that writes `files`, the
    fakes, engine runs, and what the owner does."""

    def __init__(self, files, contexts=()):
        self.repo = TempRepo()
        self.main_moves({WORKFLOW: "jobs: old\n"})
        self.fake = L.github.FakeGitHub(on_merge=self.repo.merge)
        body = issue_body(BRANCH, [f"`{path}` contains `{text}`." for path, text in files.items()],
                          ["The test passes on main."])
        self.fake.add_issue(ISSUE, "Issue 2", body, labels=("size:M",))
        self.fake.add_issue(EPIC, "Epic", "An epic.\n", subs=(ISSUE,))
        self.agents = MainMovesFirst({ISSUE: L.agents.Script(files=dict(files))})
        self.deploy = L.config.Deploy(contexts=tuple(contexts), timeout_s=2)
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)

    def run(self):
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)
        engine = L.engine.Engine(
            L.graph.build_plan(self.fake, EPIC, self.repo.work), self.store, self.fake,
            L.workspace.Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.tmp / "loop"), self.agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None,
            policy={"lacks": [], "deploy": self.deploy})
        return engine.run()

    # what the engine left
    def issue(self):
        return self.store.issue(ISSUE)

    def waits(self):
        """(phase, kind of wait, phase it resumes in, detail)"""
        st = self.issue()
        w = st.wait or {}
        return st.phase, w.get("kind"), w.get("resume"), w.get("detail", "")

    def events(self, kind):
        return [e for e in self.store.events() if e["kind"] == kind and e.get("issue") == ISSUE]

    def calls(self, name):
        return [c for c in self.fake.calls if c[0] == name]

    def agent_calls(self, role):
        return [c for c in self.agents.calls if c[0] == role]

    def handed_over(self):
        """The comments on the issue that give the owner a head, oldest first."""
        return [c["body"] for c in self.fake.comments.get(ISSUE, []) if MARKER in c["body"]]

    def remote(self):
        """The remote's branches and their heads."""
        listed = git("ls-remote", "--heads", str(self.repo.remote), cwd=self.repo.tmp).splitlines()
        return {ref.removeprefix("refs/heads/"): sha for sha, ref in (line.split("\t") for line in listed)}

    def pulls(self, branch=BRANCH):
        return [p for p in self.fake.prs.values() if p["head"]["ref"] == branch]

    def no_fault(self):
        return (not [e for e in self.store.events() if e["kind"] == "step_crashed"]
                and not L.state.FaultOutbox(self.repo.work).pending() and not self.calls("create_issue"))

    def queue_free(self):
        root = L.state.runs_root(self.repo.work)
        return not (root / "claims" / "merge-queue.claim").exists() and not list((root / L.engine.DEPLOY_HOLDS).glob("*"))

    # the world outside the engine
    def main_moves(self, files):
        clone = self.repo.merger
        git("fetch", "-q", "origin", cwd=clone)
        git("checkout", "-q", "main", cwd=clone)
        git("reset", "-q", "--hard", "origin/main", cwd=clone)
        for path, text in files.items():
            (clone / path).parent.mkdir(parents=True, exist_ok=True)
            (clone / path).write_text(text)
        git("add", "--all", cwd=clone)
        git("commit", "-q", "-m", f"main moves: {', '.join(files)}", cwd=clone)
        git("push", "-q", "origin", "HEAD:main", cwd=clone)
        return git("rev-parse", "HEAD", cwd=clone)

    def owner_pushes(self, comment):
        """Run the command the comment gives, as it is, where the engine runs. git refuses it
        unless it fast-forwards the remote branch: nothing here forces."""
        command = present(re.search(r"```sh\n(git push [^\n]+)\n```", comment)).group(1)
        subprocess.run(shlex.split(command), cwd=self.repo.work, check=True, capture_output=True)

    def owner_merges(self, pr, status=None):
        """Squash the pull request's branch into the remote's main, as GitHub's button does for the
        owner; the pipeline then posts `status` on the merge commit."""
        branch = pr["head"]["ref"]
        head = self.remote()[branch]
        commit = self.repo.merge(branch, head)
        pr.update(state="closed", merged=True, merge_commit_sha=commit)
        pr["head"]["sha"] = head
        if status:
            self.fake.set_status(commit, DEPLOY, status)
        return commit


@contextmanager
def loop(files, **kwargs):
    run = Loop(files, **kwargs)
    try:
        yield run
    finally:
        run.repo.cleanup()


def test_a_workflow_change_is_handed_to_the_owner():
    with loop({WORKFLOW: "jobs: new"}) as run:
        run.run()

        st = run.issue()
        head = present(st.certified_sha)
        assert run.remote() == {"main": run.remote()["main"]}, "nothing was pushed"
        assert run.waits()[:3] == (Phase.WAITING, OWNER_LANDING, Phase.LAND.value)
        record = present(st.owner_landing)
        assert (record["branch"], record["head"], record["paths"]) == (BRANCH, head, [WORKFLOW])
        assert run.queue_free(), "the landing claim is released, and no deployment is awaited"
        assert not run.calls("create_pr") and not run.calls("merge_pr")
        assert run.no_fault()
        [comment] = run.handed_over()
        assert BRANCH in comment and head in comment
        assert f"git push origin {head}:refs/heads/{BRANCH}" in comment
        assert "you merge it" in comment and "does not merge" in comment
        assert "Do not bring the branch up to date on GitHub" in comment
        progress = next(c["body"] for c in run.fake.comments[ISSUE] if L.report.PROGRESS_MARKER in c["body"])
        assert f"Waiting ({OWNER_LANDING})" in progress and L.report.WAIT_HINT[OWNER_LANDING] in progress

        run.run()   # resumed, and the owner has not pushed

        phase, kind, _, detail = run.waits()
        assert (phase, kind) == (Phase.WAITING, OWNER_LANDING)
        assert f"the remote has no branch `{BRANCH}`" in detail
        assert len(run.handed_over()) == 1, "one comment for a head, however often the issue is resumed"
        assert not run.calls("create_pr") and "main" in run.remote() and len(run.remote()) == 1 and run.no_fault()


def test_the_engine_opens_but_never_merges():
    with loop({WORKFLOW: "jobs: new"}, contexts=(DEPLOY,)) as run:
        run.run()
        head = present(run.issue().certified_sha)
        run.owner_pushes(run.handed_over()[0])
        assert run.remote()[BRANCH] == head

        run.run()

        st = run.issue()
        [pr] = run.pulls()
        assert len(run.calls("create_pr")) == 1 and st.pr == pr["number"] and pr["state"] == "open"
        assert pr["body"] == L.report.render_pr_body(ISSUE, "Issue 2", st, "claude", "codex")
        [evidence] = [c["body"] for c in run.fake.comments[pr["number"]]]
        assert evidence == L.report.render_evidence(ISSUE, st.evidence, head, st.verdicts)
        phase, kind, resume, detail = run.waits()
        assert (phase, kind, resume) == (Phase.WAITING, OWNER_LANDING, Phase.LAND.value)
        assert f"#{pr['number']} is open" in detail and "the owner merges it" in detail
        assert not run.calls("merge_pr") and run.queue_free()

        run.run()   # resumed, and the owner has not merged

        assert len(run.pulls()) == 1 and len(run.calls("create_pr")) == 1 and not run.calls("merge_pr")
        assert run.waits()[1] == OWNER_LANDING

        commit = run.owner_merges(pr, status="success")
        run.run()

        st = run.issue()
        assert st.phase == Phase.DONE and st.owner_landing is None
        assert not run.calls("merge_pr"), "the owner merged it: the engine never asked GitHub to"
        assert len(run.pulls()) == 1 and len(run.handed_over()) == 1
        assert {e["head_sha"] for e in st.evidence if e["kind"] == "command"} == {pr["head"]["sha"]}, \
            "the evidence is the verifier's, on the head the owner merged, as for any issue"
        assert [e["head_sha"] for e in st.evidence if e["kind"] == "deployment"] == [commit], "and so did the verdict"
        assert run.fake.issues[ISSUE]["state"] == "closed" and "- [ ]" not in run.fake.issues[ISSUE]["body"]
        assert run.repo.main_file(WORKFLOW) == "jobs: new"
        assert run.queue_free() and run.no_fault()


def test_another_head_keeps_waiting():
    with loop({WORKFLOW: "jobs: new"}) as run:
        run.run()
        head = present(run.issue().certified_sha)
        other = git("rev-parse", f"{head}~1", cwd=run.repo.work)
        git("push", "-q", "origin", f"{other}:refs/heads/{BRANCH}", cwd=run.repo.work)

        run.run()

        phase, kind, _, detail = run.waits()
        assert (phase, kind) == (Phase.WAITING, OWNER_LANDING)
        assert other[:12] in detail and head[:12] in detail and "another head" in detail
        assert not run.calls("create_pr") and not run.pulls() and not run.calls("merge_pr")
        assert run.remote()[BRANCH] == other and len(run.handed_over()) == 1 and run.no_fault()


def test_a_conflict_with_main_is_certified_again():
    with loop({WORKFLOW: "jobs: new", "docs/2.txt": "text"}) as run:
        run.run()
        first = present(run.issue().certified_sha)
        run.owner_pushes(run.handed_over()[0])
        run.run()
        [pr] = run.pulls()
        verified, examined, built = len(run.events("verified")), len(run.agent_calls("examine")), len(run.agent_calls("build"))
        main = run.main_moves({"docs/2.txt": "what another issue wrote\n"})
        pr["mergeable"] = False   # as GitHub says of it from then on

        run.run()

        st = run.issue()
        second = present(st.certified_sha)
        repo = run.repo.work
        assert run.events("owner_landing_conflict")[0]["files"] == ["docs/2.txt"]
        assert len(run.agent_calls("build")) == built + 1, "the builder resolved the conflict, in one round"
        assert "conflicts with this branch" in " ".join(run.agent_calls("build")[-1][3])
        assert len(run.events("verified")) > verified and len(run.agent_calls("examine")) > examined
        assert second != first and st.verified_sha == second
        assert L.workspace.Workspace(repo, EPIC, SHIP).is_ancestor(repo, first, second), "it descends from the old head"
        assert L.workspace.Workspace(repo, EPIC, SHIP).is_ancestor(repo, main, second), "and holds main's merge"
        assert present(st.owner_landing)["head"] == second
        earlier, comment = run.handed_over()
        assert first in earlier and second in comment and first[:12] in comment
        assert f"git push origin {second}:refs/heads/{BRANCH}" in comment
        phase, kind, _, detail = run.waits()
        assert (phase, kind) == (Phase.WAITING, OWNER_LANDING) and first[:12] in detail and second[:12] in detail
        assert run.remote()[BRANCH] == first, "the engine did not update the remote branch itself"
        assert not run.calls("merge_pr") and run.no_fault()

        run.owner_pushes(comment)   # a fast-forward of the remote branch
        run.run()

        assert run.remote()[BRANCH] == second and run.pulls() == [pr] and len(run.calls("create_pr")) == 1
        assert run.waits()[1] == OWNER_LANDING and f"#{pr['number']} is open" in run.waits()[3]
        assert second[:12] in run.fake.comments[pr["number"]][0]["body"], "the evidence is the new head's"

        run.owner_merges(pr)
        run.run()

        assert run.issue().phase == Phase.DONE and not run.calls("merge_pr") and len(run.handed_over()) == 2
        assert run.repo.main_file(WORKFLOW) == "jobs: new" and run.repo.main_file("docs/2.txt") == "text"


def test_a_clean_move_of_main_certifies_nothing():
    with loop({WORKFLOW: "jobs: new"}) as run:
        run.run()
        st = run.issue()
        head, wt = present(st.certified_sha), Path(present(st.worktree))
        verified, examined, built = len(run.events("verified")), len(run.agent_calls("examine")), len(run.agent_calls("build"))
        main = run.main_moves({"docs/other.txt": "what another issue wrote\n"})

        run.run()

        st = run.issue()
        assert (len(run.events("verified")), len(run.agent_calls("examine")), len(run.agent_calls("build"))) == (
            verified, examined, built), "no new verification, examination or round"
        assert st.certified_sha == head and present(st.owner_landing)["head"] == head
        assert git("rev-parse", "HEAD", cwd=wt) == head and not git("status", "--porcelain", cwd=wt)
        assert git("rev-parse", "origin/main", cwd=wt) == main, "the engine saw the new main"
        assert len(run.handed_over()) == 1 and run.waits()[1] == OWNER_LANDING and not run.events("conflict")

        run.owner_pushes(run.handed_over()[0])   # the same head, pushed and merged as it is
        run.run()
        run.owner_merges(run.pulls()[0])
        run.run()

        assert run.issue().phase == Phase.DONE and not run.calls("merge_pr")
        assert (len(run.events("verified")), len(run.agent_calls("examine"))) == (verified, examined)
        assert run.repo.main_file(WORKFLOW) == "jobs: new" and run.repo.main_file("docs/other.txt")


def test_other_changes_land_as_before():
    with loop({".github/dependabot.yml": "updates"}) as run:
        run.run()

        st = run.issue()
        [pr] = run.pulls()
        assert st.phase == Phase.DONE and st.owner_landing is None
        assert run.remote()[BRANCH] == st.certified_sha, "the engine pushed the branch"
        assert run.calls("merge_pr") == [("merge_pr", pr["number"], st.certified_sha)], "and merged its pull request"
        assert not run.handed_over() and not run.events("owner_landing") and not run.events("waiting")
        assert run.repo.main_file(".github/dependabot.yml") == "updates"


def test_a_merge_of_a_main_that_changed_a_workflow_file_is_still_pushed():
    with loop({"docs/2.txt": "text"}) as run:
        # main changes its workflow file while the head is examined: the land step merges that main
        run.agents.during_examination = lambda: run.main_moves({WORKFLOW: "jobs: newer\n"})
        run.run()

        st = run.issue()
        assert st.phase == Phase.DONE and run.events("updated_from_main"), "the branch merged main's workflow change"
        assert run.remote()[BRANCH] == st.certified_sha and len(run.calls("merge_pr")) == 1
        assert not run.handed_over() and not run.events("owner_landing")
        assert run.repo.main_file(WORKFLOW) == "jobs: newer"


def test_a_revert_of_a_workflow_change_is_handed_to_the_owner():
    with loop({WORKFLOW: "jobs: new"}, contexts=(DEPLOY,)) as run:
        run.run()
        run.owner_pushes(run.handed_over()[0])
        run.run()
        [pr] = run.pulls()
        run.owner_merges(pr, status="failure")   # the deployment of the merged workflow change fails

        run.run()   # the fix-forward changes nothing, so the revert is made and certified

        st = run.issue()
        revert = f"{BRANCH}-r2-revert"   # made in the fix-forward round, which landed nothing
        assert len(run.events("fix_forward")) == 1 and len(run.events("deployment_failed")) == 2
        record = present(st.owner_landing)
        assert (record["branch"], record["revert"], record["of"]) == (revert, True, [pr["number"]])
        assert record["paths"] == [WORKFLOW]
        assert run.waits()[:3] == (Phase.WAITING, OWNER_LANDING, Phase.MERGED.value)
        assert set(run.remote()) == {"main", BRANCH}, "the revert was not pushed"
        assert not run.pulls(revert) and not run.calls("merge_pr") and not run.events("reverted")
        assert run.queue_free() and run.no_fault()
        _, comment = run.handed_over()
        assert f"git push origin {record['head']}:refs/heads/{revert}" in comment and "you merge it" in comment
        assert git("show", f"{record['head']}:{WORKFLOW}", cwd=run.repo.work) == "jobs: old"

        run.run()   # resumed, and the owner has not pushed

        assert run.waits()[1] == OWNER_LANDING and f"no branch `{revert}`" in run.waits()[3]
        assert not run.pulls(revert) and len(run.handed_over()) == 2

        run.owner_pushes(comment)
        run.run()

        [reverting] = run.pulls(revert)
        assert reverting["state"] == "open" and f"#{pr['number']}" in reverting["body"]
        assert run.waits()[:3] == (Phase.WAITING, OWNER_LANDING, Phase.MERGED.value) and not run.calls("merge_pr")

        run.owner_merges(reverting)
        assert run.repo.main_file(WORKFLOW) == "jobs: old"
        run.run()   # the issue is delivered again, and its change is the owner's to land again

        st = run.issue()
        [reverted] = run.events("reverted")
        assert (reverted["pr"], reverted["of"], reverted["by"]) == (reverting["number"], [pr["number"]], "owner")
        record = present(st.owner_landing)
        assert st.round == 3 and record["branch"] == st.branch == f"{BRANCH}-r3" and not record.get("revert")
        assert run.waits()[:3] == (Phase.WAITING, OWNER_LANDING, Phase.LAND.value)
        assert set(run.remote()) == {"main", BRANCH, revert}, "only what the owner pushed is on the remote"
        assert not run.calls("merge_pr") and len(run.handed_over()) == 3 and run.no_fault()
