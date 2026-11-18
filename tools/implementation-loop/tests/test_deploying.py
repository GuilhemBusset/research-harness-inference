"""The deployment verdict between the merge and the evidence step (ADR 0001, decision 5 and
amendment 4; issue #12): `deploy_status`, the table `[deploy]` of loop.toml, the phase `deploying`
and its two waits. And what a verified deployment discharges (decision 6; issue #143): an item
that needs a deployed environment and names a smoke test.

The engine runs are real (git, the verifier, squash merges into a local remote). The agents and
GitHub are fakes; the fake GitHub also plays the pipeline, which posts a commit status on each
merge commit as the test scripts it."""

import importlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import SHIP, TempRepo, issue_body, present
from implement_loop import config as loaded

HERE = Path(__file__).resolve().parent
EPIC = 1
DEPLOY, VERIFY = "deploy/beta", "verify/beta"
APPROVAL = "waiting for the owner's approval"
# The engine compares a description with its configuration and imports no reporter: any text does here.
VERIFIED, PARKED = "the verification's tests ran and passed", "the system is parked: no test ran"
DEPLOYED, GPU = "aws:deployed-environment", "gpu:capacity"
SMOKE = "tests/smoke/deployed/test_sign_in.py"
SIGNS_IN = f"`{SMOKE}::test_a_student_signs_in` passes on the deployed endpoint (needs aws:deployed-environment)."


def loop_beside_these_tests() -> SimpleNamespace:
    """The modules of the `implement_loop` beside these tests, which is the one they judge. The
    verifier's pytest plugin belongs to the engine that runs the loop, and loading it imports that
    engine's `implement_loop`. It drops the package again only in the pytest that records, not in
    an xdist worker, so the tests of `scripts/check.sh engine-tests` would find there the running
    engine: one without the phase, while this change is built. There, the package beside these
    tests is loaded whole under another name, with the two defaults `helpers.py` sets."""
    mine = HERE.parent / "implement_loop"
    name = beside = "implement_loop"
    if Path(present(loaded.__file__)).resolve().parent != mine:
        name = "implement_loop_beside_test_deploying"
        spec = present(importlib.util.spec_from_file_location(name, mine / "__init__.py",
                                                              submodule_search_locations=[str(mine)]))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        present(spec.loader).exec_module(sys.modules[name])
    modules = SimpleNamespace(**{m: importlib.import_module(f"{name}.{m}") for m in (
        "agents", "config", "engine", "github", "graph", "model", "report", "sandbox", "state", "workspace")})
    if name != beside:
        modules.sandbox.use(modules.sandbox.PassThrough())
        modules.config.STANDING_CHECKS = modules.config.HISTORY_CHECKS = ()
    return modules


L = loop_beside_these_tests()
Phase = L.model.Phase


def wait_until(condition, seconds=60):
    """Hold a fake's thread until the engine reached the point the test is about."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the engine never reached the point this test waits for"
        time.sleep(0.02)


class Pipeline(L.github.FakeGitHub):
    """The fake GitHub, and the pipeline behind it. `posts[n]` is what the pipeline posts on each
    merge commit of issue `n`, in order: a state for every configured context, a (state,
    description) pair, a dict per context, or None for no status; past the list, nothing."""

    def __init__(self, repo, contexts):
        super().__init__(on_merge=self._merge)
        self.repo, self.contexts = repo, contexts
        self.posts: dict[int, list] = {}
        self.commits: dict[int, list[str]] = {}   # issue -> its merge commits (a revert's is not one)
        self.reads: list[tuple[str, str]] = []
        self.before_read = lambda: None

    def _merge(self, branch, sha):
        commit = self.repo.merge(branch, sha)
        n = int(present(re.search(r"issue-(\d+)", branch)).group(1))
        if not branch.endswith("-revert"):
            self.commits.setdefault(n, []).append(commit)
            self.post(commit, self.posts[n].pop(0) if self.posts.get(n) else None)
        return commit

    def post(self, commit, entry):
        per_context = entry if isinstance(entry, dict) else dict.fromkeys(self.contexts, entry)
        for context, status in per_context.items():
            if status:
                self.set_status(commit, context, *([status] if isinstance(status, str) else status))

    def deploy_status(self, sha, context):
        self.before_read()
        self.reads.append((sha, context))
        return super().deploy_status(sha, context)

    def merges(self, n=None):
        """The merges GitHub made: of issue `n`'s pull requests, or all of them."""
        mine = {p["number"] for p in self.prs.values() if n is None or re.search(rf"issue-{n}(-r\d+)?$", p["head"]["ref"])}
        return [c for c in self.calls if c[0] == "merge_pr" and c[1] in mine]


class Agents(L.agents.FakeAgents):
    """The fake agents, with two additions. The build of an issue in `after` starts once the issue
    it names has merged, or once the condition it gives holds, so the order of two landings is the
    test's and not a race. And a round
    whose feedback speaks of the deployment changes a file of its own, unless `repairs` is off: a
    fix-forward then has something to land."""

    def __init__(self, scripts, after, repairs, merged, needs=None):
        super().__init__(scripts)
        self.after, self.repairs, self.merged = after, repairs, merged
        self.needs = needs or {}   # (issue, item) -> the capability the plan gives that owner item

    def plan(self, vendor, req, worktree):
        result = super().plan(vendor, req, worktree)
        result.zones.append(f"docs/repair_{req.issue.number}.txt")
        for item in result.items:
            if item.proof == "owner":
                item.capability = self.needs[req.issue.number, item.id]
        return result

    def build(self, vendor, req, plan, worktree, feedback):
        n = req.issue.number
        if n in self.after:
            after = self.after[n]
            wait_until(after if callable(after) else lambda: self.merged(after))
        result = super().build(vendor, req, plan, worktree, feedback)
        if self.repairs and any("deployment" in f for f in feedback):
            (worktree / f"docs/repair_{n}.txt").write_text("repaired\n")
        return result


class Loop:
    """A throwaway repository, the fake GitHub with its pipeline, and engine runs with `[deploy]` set."""

    def __init__(self, contexts=(DEPLOY,), timeout_s=2, approval="", verified="", lacks=()):
        self.repo = TempRepo()
        self.deploy = L.config.Deploy(contexts=tuple(contexts), timeout_s=timeout_s, approval_description=approval,
                                      verified_description=verified)
        self.lacks = list(lacks)
        self.fake = Pipeline(self.repo, self.deploy.contexts)
        self.scripts = {}
        self.needs: dict[tuple[int, str], str] = {}
        self.after: dict[int, int | Callable[[], bool]] = {}
        self.repairs = True
        self.stores = {}
        self.root = L.state.runs_root(self.repo.work)

    def add_work(self, *numbers, epic=EPIC, owner=(), files=()):
        """One issue per number. Its first deliverable is the agents'; then comes one owner item per
        (text, capability) of `owner`: D2, D3. The build also writes each path of `files`."""
        for n in numbers:
            path = f"docs/{n}.txt"
            body = issue_body(f"feat/issue-{n}", [f"`{path}` contains `text`.", *(text for text, _ in owner)],
                              ["The test passes on main."])
            self.fake.add_issue(n, f"Issue {n}", body, labels=("size:M",))
            self.needs.update({(n, f"D{k}"): capability for k, (_, capability) in enumerate(owner, start=2)})
            self.scripts[n] = L.agents.Script(files={path: "text", **dict.fromkeys(files, "# a smoke test")},
                                              owner_items=tuple(f"D{k}" for k in range(2, 2 + len(owner))))
        self.fake.add_issue(epic, "Epic", "An epic.\n", subs=numbers)

    def run(self, root=EPIC, **caps):
        self.agents = Agents(self.scripts, self.after, self.repairs, lambda n: bool(self.fake.commits.get(n)), self.needs)
        store = self.stores[root] = L.state.RunStore.for_root(self.repo.work, root)
        engine = L.engine.Engine(
            L.graph.build_plan(self.fake, root, self.repo.work), store, self.fake,
            L.workspace.Workspace(self.repo.work, root, SHIP, base_dir=self.repo.tmp / f"loop-{root}"), self.agents,
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0, **caps)),
            repo_root=self.repo.work, log=lambda m: None, sleep=lambda s: None,
            policy={"lacks": self.lacks, "deploy": self.deploy})
        return engine.run()

    def issue(self, n, root=EPIC):
        return self.stores[root].issue(n)

    def waits(self, n, root=EPIC):
        """(phase, kind of wait, phase it resumes in)"""
        st = self.issue(n, root)
        return st.phase, (st.wait or {}).get("kind"), (st.wait or {}).get("resume")

    def events(self, kind, n=None, root=EPIC):
        return [e for e in self.stores[root].events() if e["kind"] == kind and (n is None or e.get("issue") == n)]

    def seq(self, kind, n, root=EPIC, **match):
        """When the first such event of issue `n` was journaled."""
        return min(e["seq"] for e in self.events(kind, n, root) if all(e.get(k) == v for k, v in match.items()))

    def queue_claimed(self):
        return (self.root / "claims" / "merge-queue.claim").exists()

    def awaited(self):
        """The merge commits whose verdict holds the landing claim, run or no run."""
        holds = self.root / L.engine.DEPLOY_HOLDS
        return sorted(json.loads(p.read_text())["sha"] for p in holds.glob("*.json")) if holds.is_dir() else []

    def feedback(self, n):
        """What the builder of issue `n` was told, build by build."""
        return [" ".join(c[3]) for c in self.agents.calls if c[0] == "build" and c[2] == n]

    def comment(self, n, marker):
        return next((c["body"] for c in self.fake.comments.get(n, []) if marker in c["body"]), "")


@contextmanager
def loop(*work, **deploy):
    run = Loop(**deploy)
    try:
        if work:
            run.add_work(*work)
        yield run
    finally:
        run.repo.cleanup()


# ---------------------------------------------------------------------- deploy_status and [deploy]
def test_deploy_status_is_the_latest_status_of_the_context():
    posted = [   # as GitHub lists them: the newest first
        {"id": 31, "context": DEPLOY, "state": "success", "description": "deployed"},
        {"id": 30, "context": VERIFY, "state": "pending", "description": "smoke tests"},
        {"id": 29, "context": DEPLOY, "state": "pending", "description": "started"}]
    asked = []

    def gh(args, **kwargs):
        asked.append(args[-1])
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(posted), stderr="")
    real = L.github.GhCli("owner/repo", runner=gh)
    fake = L.github.FakeGitHub()
    for status in reversed(posted):
        fake.set_status("abc123", status["context"], status["state"], status["description"])
    for client in (real, fake):
        assert client.deploy_status("abc123", DEPLOY) == {"state": "success", "description": "deployed"}
        assert client.deploy_status("abc123", VERIFY) == {"state": "pending", "description": "smoke tests"}
        assert client.deploy_status("abc123", "deploy/other") is None
    assert all(path.startswith("repos/owner/repo/commits/abc123/statuses") for path in asked)
    assert fake.deploy_status("def456", DEPLOY) is None, "another commit's statuses are not this commit's"


def test_loop_toml_has_the_table_with_neutral_values_and_no_other_key_is_read():
    committed = L.config.load_policy(HERE.parent / "loop.toml")["deploy"]
    assert committed == L.config.Deploy(contexts=(), timeout_s=3600, approval_description="")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "loop.toml"

        def read(table):
            path.write_text(table)
            return L.config.load_policy(path)["deploy"]
        assert read("") == L.config.Deploy(), "no table: the neutral values"
        assert read(f'[deploy]\ncontexts = ["{DEPLOY}", "{VERIFY}"]\ntimeout_s = 60\napproval_description = "{APPROVAL}"\n') \
            == L.config.Deploy(contexts=(DEPLOY, VERIFY), timeout_s=60, approval_description=APPROVAL)
        with pytest.raises(ValueError, match="context"):
            read(f'[deploy]\ncontext = ["{DEPLOY}"]\n')   # misspelt: it would wait for nothing, silently
        with pytest.raises(TypeError, match="timeout_s"):
            read('[deploy]\ntimeout_s = "an hour"\n')


def test_loop_toml_has_an_empty_verified_description_and_a_set_one_is_read():
    committed = tomllib.loads((HERE.parent / "loop.toml").read_text())["deploy"]
    assert committed["verified_description"] == "", "the key is there, and the loop never fills it"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "loop.toml"
        path.write_text(f'[deploy]\nverified_description = "{VERIFIED}"\n')
        assert L.config.load_policy(path)["deploy"] == L.config.Deploy(verified_description=VERIFIED)
        path.write_text("[deploy]\nverified_description = true\n")
        with pytest.raises(TypeError, match="verified_description"):
            L.config.load_policy(path)


# ---------------------------------------------------------------------- the phase
def test_an_empty_contexts_list_waits_for_nothing():
    with loop(2, contexts=()) as run:
        run.run()
        assert run.waits(2)[0] == Phase.DONE
        assert run.fake.reads == [], "no status is read"
        assert not run.events("waiting") and not run.awaited()
        assert Phase.DEPLOYING.value not in [e["to"] for e in run.events("phase", 2)]
        assert not [e for e in run.issue(2).evidence if e["kind"] == "deployment"]


def test_success_of_every_context_is_recorded_as_evidence():
    with loop(2, contexts=(DEPLOY, VERIFY)) as run:
        run.fake.posts[2] = ["success"]
        seen = []
        run.fake.before_read = lambda: seen.append((run.comment(2, L.report.PROGRESS_MARKER), run.queue_claimed()))
        run.run()

        [commit] = run.fake.commits[2]
        st = run.issue(2)
        assert st.phase == Phase.DONE and run.fake.issues[2]["state"] == "closed"
        assert (run.seq("merged", 2) < run.seq("phase", 2, to="deploying") < run.seq("deployed", 2)
                < run.seq("phase", 2, to="done")), "merged, then deploying, then the evidence step"
        assert set(run.fake.reads) == {(commit, DEPLOY), (commit, VERIFY)}
        progress, claimed = seen[0]
        assert L.report.PHASE_TEXT[Phase.DEPLOYING] in progress, "the progress comment renders the new phase"
        assert claimed, "the landing claim is held while the statuses are read"
        [record] = [e for e in st.evidence if e["kind"] == "deployment"]
        assert record["passed"] and record["head_sha"] == commit
        assert record["contexts"] == {DEPLOY: "success", VERIFY: "success"}
        evidence = run.comment(present(st.pr), L.report.EVIDENCE_MARKER)
        assert commit[:12] in evidence and DEPLOY in evidence and VERIFY in evidence
        assert not run.queue_claimed() and not run.awaited(), "the verdict is in: the claim is free"


def test_a_failed_deployment_gets_a_fix_forward_that_names_the_context_and_its_state():
    with loop(2) as run:
        run.fake.posts[2] = [("failure", "the stack was rolled back"), "success"]
        run.run()

        first, second = run.fake.commits[2]
        assert run.events("deployment_failed", 2)[0]["contexts"] == {DEPLOY: "failure"}
        assert len(run.events("fix_forward", 2)) == 1 and not run.events("reverted", 2)
        told = run.feedback(2)[1]
        assert DEPLOY in told and "failure" in told and first[:12] in told
        assert "the stack was rolled back" in told, "and the description of the context's status (issue #23)"
        assert len(run.fake.merges(2)) == 2
        assert run.waits(2)[0] == Phase.DONE
        assert [e["head_sha"] for e in run.issue(2).evidence if e["kind"] == "deployment"] == [second]


def test_an_error_that_the_fix_forward_does_not_repair_ends_in_a_revert_pull_request():
    with loop(2) as run:
        run.repairs = False   # the fix-forward round changes nothing: main's head is still the commit that failed
        run.fake.posts[2] = ["error", "success"]
        run.run()

        assert [e["contexts"] for e in run.events("deployment_failed", 2)] == [{DEPLOY: "error"}] * 2
        assert len(run.events("fix_forward", 2)) == 1
        [reverted] = run.events("reverted", 2)
        revert = run.fake.prs[reverted["pr"]]
        assert revert["merged"] and revert["head"]["ref"].endswith("-revert")
        assert "error" in run.feedback(2)[1]
        assert run.waits(2)[0] == Phase.DONE, "delivered again after the revert, with a deployment that succeeds"
        assert run.repo.main_file("docs/2.txt") == "text"
        assert not run.awaited()


# ---------------------------------------------------------------------- the landing claim
def test_the_landing_claim_is_held():
    with loop(2, 3) as run:
        run.after[3] = 2            # #3 builds once #2 has merged
        run.fake.posts[3] = ["success"]
        seen = []

        def at_each_read():   # the first read of #2's status waits until #3 is ready to land
            wait_until(lambda: run.issue(3).phase == Phase.LAND or 3 in run.fake.commits)
            seen.append((run.issue(3).phase, run.queue_claimed(), len(run.fake.merges())))
        run.fake.before_read = at_each_read
        run.run(max_parallel_writers=2)   # #3 is built while #2 waits: with one writer it would start
        # only when the engine's 30-second wait for a finished call ends, as #2's call is held here

        [commit] = run.fake.commits[2]
        assert set(seen) == {(Phase.LAND, True, 1)}, "while #2 polls: #3 stays in land, the claim is held, one merge"
        assert run.waits(2) == (Phase.WAITING, "deploy", "deploying"), "no verdict within the timeout"
        assert run.waits(3) == (Phase.WAITING, "merge_queue", "land")
        assert len(run.fake.merges()) == 1 and 3 not in run.fake.commits, "#3 has not landed"
        assert run.awaited() == [commit], "the claim outlasts the run, for the supervisor's next one"

        run.fake.before_read = lambda: None
        run.fake.set_status(commit, DEPLOY, "success")
        run.run()
        assert (run.waits(2)[0], run.waits(3)[0]) == (Phase.DONE, Phase.DONE)
        assert run.seq("deployed", 2) < run.seq("merged", 3), "#3 lands only once #2 has its verdict"
        assert len(run.fake.merges(2)) == 1 and len(run.fake.merges(3)) == 1


def test_a_run_for_another_root_does_not_land_before_the_verdict():
    with loop(2) as run:
        run.add_work(3, epic=10)
        run.fake.posts[3] = ["success"]
        run.run()
        [commit] = run.fake.commits[2]
        assert run.waits(2)[1] == "deploy"
        assert not run.queue_claimed(), "the run is over: its process holds nothing"

        run.run(root=10, queue_wait_s=0)   # another run, as after a restart: what stops it is the awaited verdict
        assert run.waits(3, root=10) == (Phase.WAITING, "merge_queue", "land")
        assert 3 not in run.fake.commits and run.awaited() == [commit]

        run.fake.set_status(commit, DEPLOY, "success")
        run.run()
        assert run.waits(2)[0] == Phase.DONE and not run.awaited()
        run.run(root=10)
        assert run.waits(3, root=10)[0] == Phase.DONE


def test_an_approval_releases_the_claim():
    with loop(2, 3, approval=APPROVAL) as run:
        # #3 builds once #2 waits for the owner and has let go of the claim: built as soon as #2
        # merged, #3 could reach its landing first and find the claim still held
        run.after[3] = lambda: run.waits(2)[1] == "owner_approval" and not run.queue_claimed()
        run.fake.posts[2] = [("pending", APPROVAL)]
        run.fake.posts[3] = ["success"]
        run.run(max_parallel_writers=2)   # with one writer, #3 could take the slot first and wait on #2 forever

        [commit] = run.fake.commits[2]
        assert run.waits(2) == (Phase.WAITING, "owner_approval", "deploying")
        assert not run.queue_claimed() and not run.awaited(), "the claim is released while the owner decides"
        assert run.waits(3)[0] == Phase.DONE, "a second issue lands meanwhile"
        assert run.seq("waiting", 2, wait="owner_approval") < run.seq("merged", 3)
        assert run.fake.issues[2]["state"] == "open"

        run.fake.set_status(commit, DEPLOY, "success", "deployed")
        run.run()
        assert run.waits(2)[0] == Phase.DONE
        assert run.fake.commits[2] == [commit] and len(run.fake.merges(2)) == 1, "the same merge commit, read again"
        assert [e["head_sha"] for e in run.issue(2).evidence if e["kind"] == "deployment"] == [commit]


def test_an_approval_wait_has_no_timeout_and_a_failure_ends_it():
    with loop(2, contexts=(DEPLOY, VERIFY), timeout_s=0, approval=APPROVAL) as run:
        run.fake.posts[2] = [{DEPLOY: ("pending", APPROVAL)}, "success"]
        for _ in range(3):   # every run reads the same commit again, and the wait stays what it is
            run.run()
            assert run.waits(2) == (Phase.WAITING, "owner_approval", "deploying")
        assert len(run.events("resumed", 2)) == 2 and {e["wait"] for e in run.events("waiting", 2)} == {"owner_approval"}

        [commit] = run.fake.commits[2]
        run.fake.set_status(commit, VERIFY, "failure")   # the approval is still pending: the failure wins
        run.run()
        assert run.events("deployment_failed", 2)[0]["contexts"] == {VERIFY: "failure"}
        assert len(run.events("fix_forward", 2)) == 1 and run.waits(2)[0] == Phase.DONE


def test_an_empty_approval_description_matches_no_status():
    with loop(2, approval="") as run:
        run.fake.posts[2] = [("pending", "")]
        run.run()
        assert run.waits(2) == (Phase.WAITING, "deploy", "deploying")


# ---------------------------------------------------------------------- no verdict at the timeout
def test_a_resumed_wait_does_not_merge_again():
    with loop(2, approval=APPROVAL) as run:
        run.run()   # the merge commit has no status at all
        [commit] = run.fake.commits[2]
        assert run.waits(2) == (Phase.WAITING, "deploy", "deploying"), "waiting, not failed"
        assert commit[:12] in present(run.issue(2).wait)["detail"]
        assert run.fake.issues[2]["state"] == "open" and not run.events("failed", 2)

        run.fake.set_status(commit, DEPLOY, "pending", "deploying")   # pending, and not for an approval
        run.run()
        assert run.waits(2) == (Phase.WAITING, "deploy", "deploying")

        run.fake.set_status(commit, DEPLOY, "success", "deployed")
        run.run()
        assert run.waits(2)[0] == Phase.DONE
        assert {sha for sha, _ in run.fake.reads} == {commit}, "every run read the same merge commit"
        assert len(run.fake.merges()) == 1 and len(run.fake.prs) == 1 and len(run.events("merged", 2)) == 1


def test_statuses_that_cannot_be_read_are_no_verdict():
    with loop(2) as run:
        run.fake.posts[2] = ["success"]

        def refused():
            raise L.github.GitHubError("gh api GET the statuses failed: HTTP 403")
        run.fake.before_read = refused
        run.run()
        [commit] = run.fake.commits[2]
        assert run.waits(2) == (Phase.WAITING, "github", "deploying") and run.awaited() == [commit]
        assert not run.events("step_crashed"), "a refused read is not an engine fault"

        run.fake.before_read = lambda: None
        run.run()
        assert run.waits(2)[0] == Phase.DONE and len(run.fake.merges()) == 1


def test_the_evidence_step_is_not_reached_with_a_verdict_owed():
    with loop(2) as run:
        run.run()
        [commit] = run.fake.commits[2]
        st = run.issue(2)   # as a run killed right after the merge leaves it: merged, and nothing read yet
        st.phase, st.wait = Phase.MERGED, None
        run.stores[EPIC].put(st)
        run.run()
        assert run.waits(2) == (Phase.WAITING, "deploy", "deploying"), "not done: the verdict is still owed"
        assert run.awaited() == [commit] and run.fake.issues[2]["state"] == "open"


def test_emptying_the_contexts_frees_an_issue_that_waited_and_the_claim():
    with loop(2) as run:
        run.run()
        assert run.waits(2)[1] == "deploy" and run.awaited()
        reads = len(run.fake.reads)
        run.deploy = L.config.Deploy()   # the owner takes the contexts out of loop.toml again
        run.run()
        assert run.waits(2)[0] == Phase.DONE and len(run.fake.reads) == reads
        assert not run.awaited(), "no record is left to stop every later landing"


def test_a_run_stopped_while_it_polls_is_reported_and_resumes_in_deploying():
    with loop(2, timeout_s=3600) as run:
        run.fake.before_read = lambda: (run.stores[EPIC].dir / "STOP").write_text("stop\n")
        summary = run.run()
        [commit] = run.fake.commits[2]
        assert summary["stopped"] and summary["phases"] == {"deploying": 1}
        assert L.report.PHASE_TEXT[Phase.DEPLOYING] in run.comment(EPIC, L.report.RUN_MARKER), \
            "the run summary renders the new phase"
        assert run.awaited() == [commit]

        run.fake.before_read = lambda: None
        run.fake.set_status(commit, DEPLOY, "success")
        run.run()
        assert run.waits(2)[0] == Phase.DONE and len(run.fake.merges()) == 1


# ---------------------------------------------------------------------- what a verified deployment discharges
@contextmanager
def deployed(item=SIGNS_IN, capability=DEPLOYED, configured=VERIFIED, contexts=(DEPLOY, VERIFY), said=VERIFIED,
             in_the_commit=(SMOKE,)):
    """Issue #2 after one engine run, on a deploy table built here. Its second deliverable, D2, is
    `item`, which the plan gives to the owner for `capability`; its change holds the files
    `in_the_commit`. Every context succeeds on the merge commit, each with the description `said`."""
    with loop(contexts=contexts, verified=configured, lacks=(DEPLOYED, GPU)) as run:
        run.add_work(2, owner=((item, capability),), files=in_the_commit)
        run.fake.posts[2] = [("success", said)]
        run.summary = run.run()
        yield run


def test_a_verified_deployment_discharges_a_smoke_test_item():
    with deployed() as run:
        [commit] = run.fake.commits[2]
        st = run.issue(2)
        assert st.phase == Phase.DONE and run.fake.issues[2]["state"] == "closed", "done, not delivered"
        # proven, with the verification's status as its evidence
        [record] = [e for e in st.evidence if e["criterion"] == "D2"]
        assert record["passed"] and record["kind"] == L.engine.COMMIT_STATUS_EVIDENCE
        assert (record["head_sha"], record["context"], record["description"]) == (commit, VERIFY, VERIFIED)
        [row] = [line for line in run.comment(present(st.pr), L.report.EVIDENCE_MARKER).splitlines() if line.startswith("| D2 ")]
        assert commit[:12] in row and VERIFY in row and VERIFIED in row, "the evidence comment carries it"
        assert f"- [x] {SIGNS_IN} ([evidence](" in run.fake.issues[2]["body"]
        assert run.events("obligations_discharged", 2)[0]["items"] == {"D2": VERIFY}
        # and not listed for the owner, on the issue, in the run summary or in the pull request
        assert (st.obligations, st.human_tasks, run.summary["owner_obligations"]) == ([], [], {})
        assert "D2:" not in "\n".join(c["body"] for c in run.fake.comments.get(2, []))
        assert "D2:" not in run.fake.prs[present(st.pr)]["body"]
        assert "#2 D2" not in run.comment(EPIC, L.report.RUN_MARKER)


NO_TEST_NAMED = "The smoke tests of `tests/smoke/` pass on the deployed endpoint (needs aws:deployed-environment)."
NEEDS_GPU = f"`{SMOKE}::test_a_pool_starts` passes on the deployed endpoint (needs gpu:capacity)."


@pytest.mark.parametrize("case", [
    pytest.param({"said": PARKED}, id="another-description"),
    pytest.param({"configured": "", "said": ""}, id="no-description-configured"),
    pytest.param({"contexts": (DEPLOY,)}, id="no-verification-context"),
    pytest.param({"item": NO_TEST_NAMED}, id="no-smoke-test-named"),
    pytest.param({"in_the_commit": ()}, id="a-smoke-test-the-commit-does-not-hold"),
    pytest.param({"item": NEEDS_GPU, "capability": GPU}, id="gpu-capacity"),
    pytest.param({"item": NEEDS_GPU}, id="gpu-capacity-whatever-the-plan-says"),
    pytest.param({"item": NEEDS_GPU.replace(" (needs gpu:capacity)", ""), "capability": GPU}, id="gpu-capacity-by-the-plan-alone"),
])
def test_only_a_verified_smoke_test_item_is_discharged(case):
    item, capability = case.get("item", SIGNS_IN), case.get("capability", DEPLOYED)
    with deployed(**case) as run:
        st = run.issue(2)
        assert [e["kind"] for e in st.evidence if e["criterion"] == "deployment"] == ["deployment"], "the deployment succeeded"
        # the item is the owner's, as before
        assert st.phase == Phase.DELIVERED and run.fake.issues[2]["state"] == "open"
        assert st.obligations == [{"item": "D2", "kind": "capability", "detail": capability,
                                   "agent_part": "time it on this host"}]
        assert f"- [ ] {item}" in run.fake.issues[2]["body"]
        assert len(st.human_tasks) == 1 and st.human_tasks[0].startswith(f"D2: needs {capability}")
        assert run.summary["owner_obligations"] == {2: st.human_tasks}
        assert f"D2: needs {capability}" in run.fake.prs[present(st.pr)]["body"]
        assert not [e for e in st.evidence if e["criterion"] == "D2"] and not run.events("obligations_discharged", 2)
