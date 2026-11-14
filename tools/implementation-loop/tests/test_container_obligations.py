"""An obligation a CI job discharges (ADR 0001, amendment 5; issue #14): an item that needs
`host:container-runtime` and names a test under `tests/container/` is proven, when its issue lands,
by the check run `container-tests` on the head that was merged.

The engine runs are real (git, the verifier, squash merges into a local remote). The agents and
GitHub are fakes; the fake GitHub also plays CI, with the check run a test gives the job."""

import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import SHIP, TempRepo, issue_body
from test_deploying import L  # the `implement_loop` beside these tests (`loop_beside_these_tests`)

HERE = Path(__file__).resolve().parent
EPIC, ISSUE = 1, 2
JOB = "container-tests"
RUNTIME, DEPLOYED = "host:container-runtime", "aws:deployed-environment"
SIGN_IN = "`tests/container/test_grafana.py::test_sign_in` passes (needs host:container-runtime)."
ON_BETA = "The dashboard answers on the beta stage (needs aws:deployed-environment)."
Phase = L.model.Phase


class Agents(L.agents.FakeAgents):
    """The fake agents, whose plan gives each owner item the capability the test names for it."""

    def __init__(self, scripts, needs):
        super().__init__(scripts)
        self.needs = needs

    def plan(self, vendor, req, worktree):
        result = super().plan(vendor, req, worktree)
        for item in result.items:
            if item.proof == "owner":
                item.capability = self.needs[item.id]
        return result


class CI(L.github.FakeGitHub):
    """The fake GitHub with one more check run on every head: the CI job's, with the conclusion a
    test gives it (`job`), or none at all (None). The fake it extends answers every name with the
    required check's verdict; this one answers by name, as GitHub does, and notes each read of a
    check run other than the required one."""

    job: str | None = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.job_reads: list[tuple[str, str]] = []
        self.job_runs: dict[str, int] = {}   # head -> the id of the job's check run on it

    def required_check_run(self, sha, name):
        if name == "required":
            return super().required_check_run(sha, name)
        self.job_reads.append((sha, name))
        if name != JOB or self.job is None:
            return None
        return {"id": self.job_runs.setdefault(sha, 7001 + len(self.job_runs)), "status": "completed",
                "conclusion": self.job}


@contextmanager
def landed(*owner, check=None):
    """Issue #2 after one engine run. Its first deliverable is the agents'; then comes one owner
    item per (text, capability) of `owner`: D2, D3. The check run `container-tests` has the
    conclusion `check` on every head, or does not exist (None)."""
    repo = TempRepo()
    try:
        gh = CI(on_merge=repo.merge)
        gh.job = check
        needs = {f"D{k}": capability for k, (_, capability) in enumerate(owner, start=2)}
        body = issue_body(f"feat/issue-{ISSUE}", ["`docs/a.txt` contains `alpha`.", *(text for text, _ in owner)],
                          ["The test passes on main."])
        gh.add_issue(ISSUE, "Issue 2", body, labels=("size:M",))
        gh.add_issue(EPIC, "Epic", "An epic.\n", subs=(ISSUE,))
        scripts = {ISSUE: L.agents.Script(files={"docs/a.txt": "alpha"}, owner_items=tuple(needs))}
        store = L.state.RunStore.for_root(repo.work, EPIC)
        engine = L.engine.Engine(
            L.graph.build_plan(gh, EPIC, repo.work), store, gh,
            L.workspace.Workspace(repo.work, EPIC, SHIP, base_dir=repo.loop_dir), Agents(scripts, needs),
            L.config.Config(repo="test/test", caps=L.config.Caps(ci_poll_s=0, queue_poll_s=0)),
            repo_root=repo.work, log=lambda m: None, sleep=lambda s: None,
            policy={"lacks": [RUNTIME, DEPLOYED], "ci_jobs": {RUNTIME: JOB}})
        summary = engine.run()
        pr = next(iter(gh.prs.values()))
        yield SimpleNamespace(gh=gh, st=store.issue(ISSUE), summary=summary, pr=pr, body=gh.issues[ISSUE]["body"],
                              comments=lambda n: "\n".join(c["body"] for c in gh.comments.get(n, [])))
    finally:
        repo.cleanup()


def obligation(item, capability):
    """The obligation the plan step records for an owner item of the fake agents."""
    return {"item": item, "kind": "capability", "detail": capability, "agent_part": "time it on this host"}


def assert_still_the_owners(run, item, text, capability):
    """As before this patch: unticked, an obligation, listed for the owner on a delivered issue."""
    assert run.st.phase == Phase.DELIVERED
    assert obligation(item, capability) in run.st.obligations
    assert f"- [ ] {text}" in run.body
    assert any(task.startswith(f"{item}: needs {capability}") for task in run.st.human_tasks)
    assert run.summary["owner_obligations"] == {ISSUE: run.st.human_tasks}
    assert f"#{ISSUE} {item}: needs {capability}" in run.comments(EPIC), "the run summary lists it"
    assert not [e for e in run.st.evidence if e["criterion"] == item]
    assert run.gh.issues[ISSUE]["state"] == "open"


# ---------------------------------------------------------------------- the mapping
def test_loop_toml_maps_the_capability_to_the_job():
    policy = L.config.load_policy(HERE.parent / "loop.toml")
    assert policy["ci_jobs"] == {RUNTIME: JOB}
    assert RUNTIME in policy["lacks"], "the mapping adds no capability: the host still lacks it"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "loop.toml"
        path.write_text('[host]\nlacks = ["gpu:capacity"]\n')
        assert L.config.load_policy(path)["ci_jobs"] == {}, "no mapping: no check run is read"
        path.write_text('[host]\nci_jobs = { "host:container-runtime" = true }\n')
        with pytest.raises(TypeError, match="ci_jobs"):
            L.config.load_policy(path)


# ---------------------------------------------------------------------- when an issue lands
def test_a_succeeded_check_run_discharges_the_obligation():
    with landed((SIGN_IN, RUNTIME), (ON_BETA, DEPLOYED), check="success") as run:
        merged_head = run.pr["head"]["sha"]
        assert run.gh.job_reads == [(merged_head, JOB)], "the job's check run is read on the head that was merged"
        address = f"https://github.test/runs/{run.gh.job_runs[merged_head]}"   # beside the pull request's page
        # proven, with the check run's address as its evidence
        record, = [e for e in run.st.evidence if e["criterion"] == "D2"]
        assert (record["passed"], record["address"], record["head_sha"]) == (True, address, merged_head)
        assert f"- [x] {SIGN_IN} ([evidence]({address}))" in run.body
        assert address in run.comments(run.pr["number"]), "the evidence comment carries it"
        # and not listed for the owner, on the issue, in the run summary or in the pull request
        assert run.st.obligations == [obligation("D3", DEPLOYED)]
        assert len(run.st.human_tasks) == 1 and run.st.human_tasks[0].startswith("D3:")
        assert f"#{ISSUE} D2" not in run.comments(EPIC)
        assert "D2:" not in run.comments(ISSUE) and "D2:" not in run.pr["body"]
        # the other obligation is the owner's, as before
        assert_still_the_owners(run, "D3", ON_BETA, DEPLOYED)


@pytest.mark.parametrize("check", [None, "skipped", "failure"], ids=["absent", "skipped", "failed"])
def test_without_a_succeeded_check_run_the_obligation_stays_the_owners(check):
    with landed((SIGN_IN, RUNTIME), check=check) as run:
        assert run.gh.job_reads == [(run.pr["head"]["sha"], JOB)]
        assert run.st.obligations == [obligation("D2", RUNTIME)]
        assert_still_the_owners(run, "D2", SIGN_IN, RUNTIME)
        assert "D2: needs host:container-runtime" in run.pr["body"]


def test_an_item_that_names_no_container_test_stays_the_owners():
    text = "The image starts with the tests of `tests/container/` in place (needs host:container-runtime)."
    with landed((text, RUNTIME), check="success") as run:
        assert run.gh.job_reads == [], "nothing of the job is read for it"
        assert_still_the_owners(run, "D2", text, RUNTIME)


@pytest.mark.parametrize("check", ["success", None, "skipped"], ids=["succeeded", "absent", "skipped"])
def test_other_obligations_are_untouched(check):
    text = "`tests/container/test_grafana.py::test_sign_in` passes against the beta stage (needs aws:deployed-environment)."
    with landed((text, DEPLOYED), check=check) as run:
        assert run.gh.job_reads == [], "no job provides this capability"
        assert run.st.obligations == [obligation("D2", DEPLOYED)]
        assert_still_the_owners(run, "D2", text, DEPLOYED)


def test_an_issue_with_nothing_left_is_done():
    with landed((SIGN_IN, RUNTIME), check="success") as run:
        assert run.st.phase == Phase.DONE, "done, not delivered"
        assert run.summary["phases"] == {"done": 1}
        assert (run.st.obligations, run.st.human_tasks, run.summary["owner_obligations"]) == ([], [], {})
        assert run.gh.issues[ISSUE]["state"] == "closed"
        assert "- [ ]" not in run.body, "every box is ticked"
        assert run.gh.issues[EPIC]["state"] == "closed", "and its epic closes with it"
