"""Regression for the engine fault ed072b1fa9 (#276): issue #32 spent its ceiling of 30 calls on
a failure its builder was never shown.

The examiner's finding had a repository-wide gate as its reproduction. Where the verifier runs
it, that gate failed on every head, for files the change did not touch; in the builder's worktree
it passed. Ten builds were told that the finding "still reproduces" (and three times that a
command "failed: exit 1") and no more; nine answered that it passed for them and changed nothing.
Each build also made a request the other vendor decides again: nine decisions, each refused,
and no refusal reached the next build, because the failed verification replaced the feedback that
carried it. The dispute that closed the finding
came at the 27th call.

The engine runs are real (git, the verifier, squash merges into a bare remote); the agents and
GitHub are the fakes. The fake builder answers as #32's did, and acts on a fact once its feedback
holds it."""

import json

import pytest

from helpers import SHIP, TempRepo, issue_body
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

EPIC, ISSUE = 1, 2
ROUND = "docs/round.txt"
PRINTED = "59 diagnostics, all in files this change does not touch"
# Fails wherever it runs, whatever the builder does, and prints why and which build it ran on.
REPRODUCTION = f"echo '{PRINTED}'; cat {ROUND}; test -f docs/never.txt"
REFUSAL = "the item does not need that protected path"
HOLDS = "docs/a.txt holds:"
Phase = L.model.Phase


class Agents(L.agents.FakeAgents):
    """The fake agents, as #32's were. Every build writes its number to `ROUND`. The first
    examination leaves a blocking finding whose reproduction is `REPRODUCTION`. The builder
    disputes it from its build number `disputes` on, and only once its feedback holds what the
    verifier's run of the reproduction printed. With `asks`, a build that follows the
    examination asks for a protected path, until it is told that this was refused; the decider
    refuses with `REFUSAL`. The arbiter rules for the builder."""

    def __init__(self, scripts, disputes=2, asks=False):
        super().__init__(scripts)
        self.disputes, self.asks = disputes, asks

    def _test_script(self, n):
        lines = super()._test_script(n).splitlines()
        return "\n".join([*lines[:2], f'echo "{HOLDS} $(cat docs/a.txt)"', *lines[2:]]) + "\n"

    def build(self, vendor, req, plan, worktree, feedback):
        result = super().build(vendor, req, plan, worktree, feedback)
        k = self._built[ISSUE]
        (worktree / ROUND).write_text(f"build {k}\n")
        told = "\n".join(feedback)
        if self.asks and k > 1 and REFUSAL not in told:
            result.requests.append(L.agents.Request("protected_change", "a hook as well", paths=[".githooks/x"]))
        if k >= self.disputes and PRINTED in told:
            result.requests.append(L.agents.Request("dispute", "it fails for files this change does not touch",
                                                    finding=f"E{ISSUE}-1-1"))
        return result

    def examine(self, *args):
        result = super().examine(*args)
        for f in result.findings:
            f.reproduction = REPRODUCTION
        return result

    def decide(self, vendor, req, worktree, requests):
        super().decide(vendor, req, worktree, requests)
        return [L.agents.Decision(r["id"], False, REFUSAL) for r in requests]

    def arbitrate(self, vendor, req, dispute, worktree=None):
        super().arbitrate(vendor, req, dispute, worktree)
        return L.agents.Ruling(False, "the item as written does not require it")

    def of(self, role):
        return [c for c in self.calls if c[0] == role]

    def feedback(self, build):
        """Everything build number `build` was given as feedback, as one text."""
        return "\n".join(self.of("build")[build - 1][3])


class Loop:
    """A throwaway repository and one issue."""

    def __init__(self):
        self.repo = TempRepo()
        self.fake = L.github.FakeGitHub(on_merge=self.repo.merge)
        body = issue_body("feat/issue-2", ["`docs/a.txt` contains `alpha`."], ["The test passes on main."])
        self.fake.add_issue(ISSUE, "Issue 2", body, labels=("size:M",))
        self.fake.add_issue(EPIC, "Epic", "An epic.\n", subs=(ISSUE,))

    def run(self, agents):
        """One engine run with these agents; the issue's state at its end."""
        self.store = L.state.RunStore.for_root(self.repo.work, EPIC)
        workspace = L.workspace.Workspace(self.repo.work, EPIC, SHIP, base_dir=self.repo.loop_dir)
        L.engine.Engine(
            L.graph.build_plan(self.fake, EPIC, self.repo.work), self.store, self.fake, workspace, agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None).run()
        return self.store.issue(ISSUE)

    def failures(self):
        return [e["problems"] for e in self.store.events() if e["kind"] == "round_failed"]


@pytest.fixture
def loop():
    run = Loop()
    yield run
    run.repo.cleanup()


def test_an_open_finding_comes_with_what_the_verifiers_run_printed(loop):
    agents = Agents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, findings=1)})
    st = loop.run(agents)

    # the round after the examination: the run that made the finding blocking, on the first build
    assert PRINTED in agents.feedback(2) and "build 1" in agents.feedback(2)
    # so the dispute is made in that round, where #32's came at its 27th call
    assert (len(agents.of("build")), len(agents.of("arbitrate"))) == (2, 1)
    assert st.phase == Phase.DONE, loop.failures()


def test_after_a_failed_verification_the_builder_knows_its_refused_requests_and_the_new_run(loop):
    agents = Agents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, findings=1)}, disputes=3, asks=True)
    st = loop.run(agents)

    # the second build asked for a path and did not dispute: its verification failed
    assert any("still reproduces" in p for problems in loop.failures() for p in problems)
    third = agents.feedback(3)
    assert REFUSAL in third, "the decision on the second build's requests, and its reason"
    assert "build 2" in third, "the verifier's run of the reproduction on the second build's commit"
    # told once, it does not ask again: one decision, where #32 paid for nine
    assert (len(agents.of("build")), len(agents.of("decide"))) == (3, 1)
    assert st.phase == Phase.DONE, loop.failures()


def test_a_failed_command_comes_with_what_it_printed(loop):
    agents = Agents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, wrong_attempts=1)})
    st = loop.run(agents)

    command = f"bash {agents.test_file(ISSUE)}"
    (said,) = [f for f in agents.of("build")[1][3] if f"{HOLDS} wrong" in f]
    assert command in said, "the output is given with the command that printed it"
    assert st.phase == Phase.DONE, loop.failures()


def test_what_a_run_printed_is_no_progress_and_is_not_published(loop):
    agents = Agents({ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, findings=1)}, disputes=99)
    st = loop.run(agents)

    # every build was shown another output ("build 1", "build 2", ...): the rounds still count as the same failure
    builds = len(agents.of("build"))
    assert all(f"build {k - 1}" in agents.feedback(k) for k in range(2, builds + 1))
    assert st.phase == Phase.FAILED and st.fail["cause"] == "no_progress"
    # a command's output goes to the agents and nowhere else
    assert PRINTED not in st.fail["detail"]
    assert PRINTED not in json.dumps([loop.fake.calls, loop.fake.comments, loop.fake.issues, loop.fake.prs], default=str)
