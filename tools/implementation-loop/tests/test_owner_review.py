"""A merge GitHub refuses for want of the owner's review (ADR 0001, amendment 3; issue #19): the
engine reads the pull request's review decision, waits (`owner_review`) without counting a merge
refusal, verifies nothing again and holds no landing claim meanwhile, and merges the same certified
head once the decision is APPROVED. Every other refused merge is counted as before.

The engine runs are real (git, the verifier, squash merges into a bare remote). The agents and
GitHub are fakes; the fake GitHub refuses a merge while the review decision the test gave a pull
request owes a review, as a ruleset that asks for the owner's review does."""

import json
import subprocess
from contextlib import contextmanager

from helpers import SHIP, TempRepo, git, issue_body, present
from test_deploying import L, wait_until  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

EPIC, ISSUE, OTHER = 1, 2, 3
Phase = L.model.Phase
OWNER_REVIEW = L.model.WAIT_OWNER_REVIEW
MARKER = L.engine.OWNER_REVIEW_MARKER.format("")[:-4]   # what every such comment starts with, whatever its head


class Agents(L.agents.FakeAgents):
    """The fake agents; `before_build[n]` runs when the first build of issue `n` starts."""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.before_build = {}

    def build(self, vendor, req, plan, worktree, feedback):
        self.before_build.pop(req.issue.number, lambda: None)()
        return super().build(vendor, req, plan, worktree, feedback)


class Loop:
    """A throwaway repository, one issue per entry of `issues` that writes a file, the fakes, and
    engine runs on the same store. `review` is the decision of every pull request opened from now on."""

    def __init__(self, review, issues=(ISSUE,)):
        self.repo = TempRepo()
        self.fake = L.github.FakeGitHub(on_merge=self.repo.merge)
        self.fake.default_review = review
        for n in issues:
            body = issue_body(f"feat/issue-{n}", [f"`docs/f{n}.txt` contains `text {n}`."], ["The test passes on main."])
            self.fake.add_issue(n, f"Issue {n}", body, labels=("size:M",))
        self.fake.add_issue(EPIC, "Epic", "An epic.\n", subs=tuple(issues))
        self.agents = Agents({n: L.agents.Script(files={f"docs/f{n}.txt": f"text {n}"}) for n in issues})
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)

    def run(self):
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)
        return L.engine.Engine(
            L.graph.build_plan(self.fake, EPIC, self.repo.work), self.store, self.fake,
            L.workspace.Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.tmp / "loop"), self.agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None, policy={"lacks": []}).run()

    def waits(self, n=ISSUE):
        """(phase, kind of wait, phase it resumes in, counted merge refusals)"""
        st = self.store.issue(n)
        w = st.wait or {}
        return st.phase, w.get("kind"), w.get("resume"), st.merge_refusals

    def kinds(self, n=ISSUE):
        """The kinds of the events of issue `n`, in order."""
        return [e["kind"] for e in self.store.events() if e.get("issue") == n]

    def pull(self, n=ISSUE):
        [pr] = [p for p in self.fake.prs.values() if p["head"]["ref"] == f"feat/issue-{n}"]
        return pr

    def said(self):
        """The comments on the issue's pull request that say it waits for the owner's review."""
        return [c["body"] for c in self.fake.comments.get(self.pull()["number"], []) if MARKER in c["body"]]

    def merges(self):
        return [c for c in self.fake.calls if c[0] == "merge_pr"]

    def refuse_every_merge(self):
        def refuse(n, sha, method="squash"):
            raise L.github.GitHubError("Pull Request is not mergeable")
        self.fake.merge_pr = refuse  # a test double

    def main_moves(self, path, text):
        clone = self.repo.merger
        git("fetch", "-q", "origin", cwd=clone)
        git("reset", "-q", "--hard", "origin/main", cwd=clone)
        (clone / path).write_text(text)
        git("add", "--all", cwd=clone)
        git("commit", "-q", "-m", f"main moves: {path}", cwd=clone)
        git("push", "-q", "origin", "HEAD:main", cwd=clone)


@contextmanager
def loop(review, **kwargs):
    run = Loop(review, **kwargs)
    try:
        yield run
    finally:
        run.repo.cleanup()


def test_the_review_decision_is_read_from_graphql():
    asked = []

    def gh(args, input=None, capture_output=True, text=True):
        asked.append(args)
        decision = "REVIEW_REQUIRED" if len(asked) == 1 else None
        answer = {"data": {"repository": {"pullRequest": {"reviewDecision": decision}}}}
        return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")

    client = L.github.GhCli("owner/repo", runner=gh)
    assert client.review_decision(7) == "REVIEW_REQUIRED"
    assert client.review_decision(7) == "", "GitHub gives null where no review is asked for"
    args = asked[0]
    assert args[:2] == ["gh", "api"] and "graphql" in args
    fields = {args[i + 1].partition("=")[0]: (flag, args[i + 1].partition("=")[2])
              for i, flag in enumerate(args) if flag in ("-f", "-F")}
    assert "reviewDecision" in fields["query"][1] and "pullRequest(number: $number)" in fields["query"][1]
    assert (fields["owner"], fields["name"]) == (("-f", "owner"), ("-f", "repo"))
    assert fields["number"] == ("-F", "7"), "a typed field: the query takes an integer"

    with loop("CHANGES_REQUESTED") as run:   # and the in-memory GitHub answers with what the test gave the pull request
        pr = run.fake.create_pr("feat/x", "main", "A title", "A body")
        assert run.fake.review_decision(pr["number"]) == "CHANGES_REQUESTED"


def test_a_review_owed_waits_for_the_owner_after_one_refusal():
    for decision in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
        with loop(decision, issues=(ISSUE, OTHER)) as run:
            # The other issue is built once this one waits, and its pull request owes no review: it can
            # only land in the same run if the wait left the merge queue free.
            def once_the_first_waits(run=run):
                wait_until(lambda: run.store.issue(ISSUE).phase == Phase.WAITING)
                run.fake.default_review = ""
            run.agents.before_build[OTHER] = once_the_first_waits

            run.run()

            assert run.waits() == (Phase.WAITING, OWNER_REVIEW, Phase.LAND.value, 0), "no merge refusal is counted"
            kinds = run.kinds()
            assert kinds.count("owner_review_owed") == 1 and "merge_refused" not in kinds
            assert "verified" not in kinds[kinds.index("owner_review_owed"):], "nothing is verified again"
            head = present(run.store.issue(ISSUE).certified_sha)
            [comment] = run.said()
            assert head in comment and "waits for the owner's review" in comment
            assert run.store.issue(OTHER).phase == Phase.DONE, "the landing claim was released: the other issue landed"
            assert [c[1] for c in run.merges()] == [run.pull(OTHER)["number"]]

            calls = len(run.agents.calls)
            run.run()   # resumed, and the review is still owed

            assert run.waits() == (Phase.WAITING, OWNER_REVIEW, Phase.LAND.value, 0)
            assert run.kinds() == kinds and len(run.agents.calls) == calls, "no step ran: nothing verified, nothing asked"
            assert len(run.said()) == 1 and len(run.merges()) == 1
            assert not (L.state.runs_root(run.repo.work) / "claims" / "merge-queue.claim").exists()


def test_a_given_review_merges_the_certified_head():
    with loop("REVIEW_REQUIRED") as run:
        run.run()
        assert run.waits()[:2] == (Phase.WAITING, OWNER_REVIEW)
        head = present(run.store.issue(ISSUE).certified_sha)
        verified = run.kinds().count("verified")

        run.pull()["review_decision"] = "APPROVED"
        run.run()

        st = run.store.issue(ISSUE)
        assert st.phase == Phase.DONE and st.merge_refusals == 0
        assert run.merges() == [("merge_pr", run.pull()["number"], head)], "the head certified before the wait"
        assert run.kinds().count("verified") == verified, "and nothing was verified again before the merge"
        assert run.fake.issues[ISSUE]["state"] == "closed" and run.repo.main_file(f"docs/f{ISSUE}.txt") == f"text {ISSUE}"
        assert len(run.said()) == 1


def test_other_refusals_are_unchanged():
    for decision in ("APPROVED", "", None):   # None: the decision cannot be read
        with loop(decision or "") as run:
            run.refuse_every_merge()
            if decision is None:
                def unreadable(n):
                    raise L.github.GitHubError("gh api graphql failed")
                run.fake.review_decision = unreadable  # a test double

            run.run()

            assert run.waits() == (Phase.WAITING, "github", Phase.LAND.value, 3), decision
            kinds = run.kinds()
            assert kinds.count("merge_refused") == 3 and "owner_review_owed" not in kinds
            assert kinds.count("verified") >= 3, "each refusal sent the issue back to verification"
            assert not run.said()


def test_a_head_that_moved_is_verified_again():
    with loop("REVIEW_REQUIRED") as run:   # `main` moved during the wait: the approved head is not the one that lands
        run.run()
        first = present(run.store.issue(ISSUE).certified_sha)
        verified = run.kinds().count("verified")
        run.main_moves("NOTES.md", "main moved\n")
        run.pull()["review_decision"] = "APPROVED"

        run.run()

        st = run.store.issue(ISSUE)
        assert st.phase == Phase.DONE and st.certified_sha != first
        assert "updated_from_main" in run.kinds() and run.kinds().count("verified") > verified
        assert run.merges() == [("merge_pr", run.pull()["number"], st.certified_sha)]

    with loop("REVIEW_REQUIRED") as run:   # the pull request got another head, and still owes a review
        run.run()
        verified = run.kinds().count("verified")
        pr = run.pull()
        pr["head_sha"] = pr["head"]["sha"] = "f" * 40

        run.run()

        assert run.waits() == (Phase.WAITING, "github", Phase.LAND.value, 3), "refused for its head, as before"
        assert run.kinds().count("verified") > verified and len(run.said()) == 1 and not run.merges()
