"""What a later verification discharges (ADR 0001, decision 6): `[deploy] discharge_contexts`, read
after the fact on each run, for each delivered issue that still owes an item that needs a deployed
environment and names a smoke test, on its merge commit or on a head of `main` that holds it. No
merge waits for such a context, and nothing holds the merge queue for it.

The engine runs are real and the fakes are those of `test_deploying.py`: the fake GitHub plays the
pipeline, and the test posts the verification's status where and when it chooses."""

import dataclasses
from contextlib import contextmanager

import pytest

from helpers import git, present
from test_deploying import DEPLOYED, GPU, LEFT_OUT, NEEDS_GPU, PARKED, SIGNS_IN, SMOKE, VERIFIED, L, Loop, Phase

RHI = "verify/Rhi"
EPIC = 1
# What the discharge may call on the GitHub client: its reads, and the writes of the evidence step.
READS = {"find_pr", "deploy_status", "list_comments", "get_issue"}
WRITES = {"edit_issue", "create_comment", "update_comment"}


class Recording:
    """The engine's GitHub client while the discharge step runs: it records each method asked for,
    and raises for the one named in `failing`, as a GitHub that does not answer."""

    def __init__(self, gh, seen, failing):
        self._gh, self._seen, self._failing = gh, seen, failing

    def __getattr__(self, name):
        attr = getattr(self._gh, name)
        if not callable(attr):
            return attr
        self._seen.append(name)
        if name in self._failing:
            def unanswered(*args, **kwargs):
                raise L.github.GitHubError(f"{name}: HTTP 502")
            return unanswered
        return attr


@pytest.fixture
def discharge(monkeypatch):
    """The calls of each run's discharge step, in `calls`; a name added to `failing` is not answered."""
    record = dataclasses.make_dataclass("Record", ["calls", "failing"])([], set())
    original = L.engine.Engine._discharge_delivered

    def recorded(self):
        gh, self.gh = self.gh, Recording(self.gh, record.calls, record.failing)
        try:
            original(self)
        finally:
            self.gh = gh
    monkeypatch.setattr(L.engine.Engine, "_discharge_delivered", recorded)
    return record


@contextmanager
def delivered(*numbers, item=SIGNS_IN, capability=DEPLOYED, files=(SMOKE,), contexts=(RHI,)):
    """Issues delivered by one run with `contexts` empty: each owes `item`, D2, which the plan gives
    the owner for `capability`, and its change holds `files`. No status is posted yet."""
    run = Loop(contexts=(), verified=VERIFIED, lacks=(DEPLOYED, GPU), unverified=(LEFT_OUT,))
    run.deploy = dataclasses.replace(run.deploy, discharge_contexts=tuple(contexts))
    try:
        run.add_work(*(numbers or (2,)), owner=((item, capability),), files=files)
        run.run()
        yield run
    finally:
        run.repo.cleanup()


def push_main(run, files=(), base="origin/main", force=False):
    """A commit pushed to `main` on top of `base`, holding `files`: what landed after the issue."""
    merger = run.repo.merger
    git("fetch", "-q", "origin", cwd=merger)
    git("checkout", "-q", "-B", "later", base, cwd=merger)
    for path in (*files, "docs/later.txt"):
        (merger / path).parent.mkdir(parents=True, exist_ok=True)
        (merger / path).write_text("# later\n")
    git("add", "-A", cwd=merger)
    git("commit", "-q", "-m", "Later", cwd=merger)
    git("push", "-q", *(["--force"] if force else []), "origin", "HEAD:main", cwd=merger)
    return git("rev-parse", "HEAD", cwd=merger)


def snapshot(run, n=2):
    st = run.issue(n)
    return st.phase, st.obligations, run.fake.issues[n]["body"], run.fake.issues[n]["state"]


def test_no_merge_waits_on_a_discharge_context():
    seen = []
    run = Loop(contexts=(), verified=VERIFIED, lacks=(DEPLOYED, GPU), unverified=(LEFT_OUT,))
    run.deploy = dataclasses.replace(run.deploy, discharge_contexts=(RHI,))
    try:
        run.add_work(2, 3, owner=((SIGNS_IN, DEPLOYED),), files=(SMOKE,))
        run.after[3] = 2   # #3 is built once #2 merged
        run.fake.posts[2] = [{RHI: ("success", VERIFIED)}]   # the verification answers at once on #2's merge commit
        run.fake.before_read = lambda: seen.append(({n: run.issue(n).phase for n in (2, 3)}, run.queue_claimed(),
                                                     run.awaited()))
        run.run()
        assert len(run.fake.merges(2)) == len(run.fake.merges(3)) == 1, "both issues landed"
        for n in (2, 3):
            assert [e["kind"] for e in run.issue(n).evidence if e["criterion"] == "D1"] == ["command"], "evidence recorded"
            assert Phase.DEPLOYING.value not in [e["to"] for e in run.events("phase", n)]
        assert not run.events("waiting") and not run.awaited() and not run.queue_claimed()
        assert seen, "the discharge read the verification"
        assert all(phases == {2: Phase.DELIVERED, 3: Phase.DELIVERED} and not claimed and not held
                   for phases, claimed, held in seen), "no status is read before the issue is delivered, nor under the queue"
        assert (run.issue(2).phase, run.issue(3).phase) == (Phase.DONE, Phase.DELIVERED)
    finally:
        run.repo.cleanup()


@pytest.mark.parametrize("on", ["the-merge-commit", "a-later-main"])
def test_a_later_verification_discharges_and_the_issue_is_done(on, discharge):
    with delivered() as run:
        st = run.issue(2)
        assert st.phase == Phase.DELIVERED and [o["item"] for o in st.obligations] == ["D2"]
        [merge] = run.fake.commits[2]
        sha = merge if on == "the-merge-commit" else push_main(run)
        run.fake.set_status(sha, RHI, "success", VERIFIED)
        discharge.calls.clear()
        run.run()
        st = run.issue(2)
        assert st.phase == Phase.DONE and run.fake.issues[2]["state"] == "closed"
        [record] = [e for e in st.evidence if e["criterion"] == "D2"]
        assert record["passed"] and record["kind"] == L.engine.COMMIT_STATUS_EVIDENCE
        assert (record["head_sha"], record["context"], record["description"]) == (sha, RHI, VERIFIED)
        [comment] = [c for c in run.fake.comments[present(st.pr)] if L.report.EVIDENCE_MARKER in c["body"]]
        [row] = [line for line in comment["body"].splitlines() if line.startswith("| D2 ")]
        assert sha[:12] in row and RHI in row and VERIFIED in row
        assert f"- [x] {SIGNS_IN} ([evidence]({comment['html_url']}))" in run.fake.issues[2]["body"]
        assert (st.obligations, st.human_tasks) == ([], [])
        assert run.events("obligations_discharged", 2)[-1]["items"] == {"D2": RHI}
        assert set(discharge.calls) <= READS | WRITES, "no kind of write the evidence step does not make"


@pytest.mark.parametrize("case", [
    pytest.param({"said": PARKED}, id="another-description"),
    pytest.param({"state": "pending"}, id="pending"),
    pytest.param({"state": "failure"}, id="failed"),
    pytest.param({"item": SIGNS_IN.replace(SMOKE, LEFT_OUT), "files": (LEFT_OUT,)}, id="a-test-among-unverified-tests"),
    pytest.param({"files": ()}, id="a-test-the-commit-does-not-hold"),
    pytest.param({"files": (f"{SMOKE}/test_inside.py",)}, id="a-directory-of-the-test-s-name"),
    pytest.param({"item": NEEDS_GPU}, id="another-capability"),
    pytest.param({"item": NEEDS_GPU, "capability": GPU}, id="another-capability-by-the-plan-too"),
    pytest.param({"context": "verify/Other"}, id="a-context-not-among-discharge-contexts"),
    pytest.param({"context": "deploy/Rhi"}, id="the-stage-s-deployment-context"),
    pytest.param({"on": "a-main-without-it"}, id="a-commit-whose-history-does-not-hold-the-merge-commit"),
    pytest.param({"failing": "deploy_status"}, id="an-unanswered-read"),
])
def test_what_discharges_nothing(case, discharge):
    with delivered(item=case.get("item", SIGNS_IN), capability=case.get("capability", DEPLOYED),
                   files=case.get("files", (SMOKE,))) as run:
        before = snapshot(run)
        assert before[0] == Phase.DELIVERED and len(before[1]) == 1
        [sha] = run.fake.commits[2]
        if case.get("on"):   # main rewritten from before the merge, with the test file in it
            sha = push_main(run, files=(SMOKE,), base="origin/main~1", force=True)
        run.fake.set_status(sha, case.get("context", RHI), case.get("state", "success"), case.get("said", VERIFIED))
        discharge.failing.update({case["failing"]} if "failing" in case else set())
        run.run()
        assert snapshot(run) == before, "the obligation and the box are as they were"
        assert not run.events("obligations_discharged", 2)
        assert not [e for e in run.issue(2).evidence if e["criterion"] == "D2"]


@pytest.mark.parametrize("failing", ["find_pr", "deploy_status", "list_comments", "update_comment", "get_issue"])
def test_an_unanswered_read_or_write_is_tried_again_by_the_next_run(failing, discharge):
    with delivered() as run:
        before = snapshot(run)
        [merge] = run.fake.commits[2]
        run.fake.set_status(merge, RHI, "success", VERIFIED)
        discharge.failing.add(failing)
        run.run()
        assert snapshot(run) == before and run.events("discharge_unread", 2)
        discharge.failing.clear()
        run.run()
        assert run.issue(2).phase == Phase.DONE and run.fake.issues[2]["state"] == "closed"
        pr = present(run.issue(2).pr)
        assert len([c for c in run.fake.comments[pr] if L.report.EVIDENCE_MARKER in c["body"]]) == 1, \
            "a list of comments that was not read is not an empty one"
