"""What a later verification discharges (ADR 0001, decision 6): `[deploy] discharge_contexts`, read
after the fact on each run, for each delivered issue that still owes an item that needs a deployed
environment and names a smoke test, on its merge commit or on a head of `main` that holds it. No
merge waits for such a context, and nothing holds the merge queue for it.

The engine runs are real and the fakes are those of `test_deploying.py`: the fake GitHub plays the
pipeline, and the test posts the verification's status where and when it chooses."""

import dataclasses
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from helpers import git, present
from test_deploying import DEPLOYED, GPU, LEFT_OUT, NEEDS_GPU, PARKED, SIGNS_IN, SMOKE, VERIFIED, L, Loop, Phase

RHI = "verify/Rhi"
EPIC = 1
# What the discharge may call on the GitHub client: its reads, and the writes of the evidence step.
READS = {"get_pr", "deploy_status", "list_comments", "get_issue"}
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


def push_main(run, files=(), base="origin/main", force=False, links=(), removed=()):
    """A commit pushed to `main` on top of `base`, holding `files`, each of `links` as a symbolic
    link to a file that does not exist, and none of `removed`: what landed after the issue."""
    merger = run.repo.merger
    git("fetch", "-q", "origin", cwd=merger)
    git("checkout", "-q", "-B", "later", base, cwd=merger)
    for path in (*files, *links, "docs/later.txt"):
        (merger / path).parent.mkdir(parents=True, exist_ok=True)
        if path in links:
            (merger / path).symlink_to("missing.py")
        else:
            (merger / path).write_text("# later\n")
    for path in removed:
        (merger / path).unlink()
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
    pytest.param({"files": (), "on": "a-link"}, id="a-symbolic-link-to-no-file"),
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
        if case.get("on") == "a-link":   # a later main whose only entry at the test's path is a dangling link
            sha = push_main(run, links=(SMOKE,))
        elif case.get("on"):   # main rewritten from before the merge, with the test file in it
            sha = push_main(run, files=(SMOKE,), base="origin/main~1", force=True)
        run.fake.set_status(sha, case.get("context", RHI), case.get("state", "success"), case.get("said", VERIFIED))
        discharge.failing.update({case["failing"]} if "failing" in case else set())
        run.run()
        assert snapshot(run) == before, "the obligation and the box are as they were"
        assert not run.events("obligations_discharged", 2)
        assert not [e for e in run.issue(2).evidence if e["criterion"] == "D2"]


@pytest.mark.parametrize("failing", ["get_pr", "deploy_status", "list_comments", "update_comment", "get_issue"])
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


@pytest.mark.parametrize("then", ["the-status-failed", "the-context-was-removed"])
def test_a_proof_never_published_is_read_again_and_must_still_hold(then, discharge):
    with delivered() as run:
        before = snapshot(run)
        [merge] = run.fake.commits[2]
        run.fake.set_status(merge, RHI, "success", VERIFIED)
        discharge.failing.add("list_comments")   # saved, then neither its row nor its tick is written
        run.run()
        discharge.failing.clear()
        if then == "the-status-failed":
            run.fake.set_status(merge, RHI, "failure", "Verification failed.")
        else:
            run.deploy = dataclasses.replace(run.deploy, discharge_contexts=("verify/Other",))
        run.run()
        assert snapshot(run) == before and not run.events("obligations_discharged", 2)


def test_another_root_s_proof_never_published_ticks_nothing(discharge):
    other = "tests/smoke/deployed/test_other.py"
    owner = ((SIGNS_IN, DEPLOYED), (SIGNS_IN.replace(SMOKE, other), DEPLOYED))
    run = Loop(contexts=(), verified=VERIFIED, lacks=(DEPLOYED,), unverified=())
    run.deploy = dataclasses.replace(run.deploy, discharge_contexts=(RHI,))
    try:
        run.add_work(2, owner=owner, files=(SMOKE,))
        run.run(root=EPIC)
        run.run(root=2)
        [merge] = run.fake.commits[2]
        run.fake.set_status(merge, RHI, "success", VERIFIED)
        discharge.failing.add("list_comments")   # the epic's run saves D2's proof and publishes nothing of it
        run.run(root=EPIC)
        discharge.failing.clear()
        run.deploy = dataclasses.replace(run.deploy, unverified_tests=(SMOKE,))   # D2's test is now left out
        run.fake.set_status(merge, RHI, "failure", "Verification failed.")
        run.fake.set_status(push_main(run, files=(other,), removed=(SMOKE,)), RHI, "success", VERIFIED)
        run.run(root=2)   # proves D3
        body = run.fake.issues[2]["body"]
        assert f"- [x] {owner[1][0]}" in body and f"- [ ] {SIGNS_IN}" in body
        assert run.fake.issues[2]["state"] == "open" and [o["item"] for o in run.issue(2, root=2).obligations] == ["D2"]
    finally:
        run.repo.cleanup()


def test_an_issue_edited_while_the_statuses_are_read_is_left_as_it_is():
    with delivered() as run:
        [merge] = run.fake.commits[2]
        run.fake.set_status(merge, RHI, "success", VERIFIED)
        issue, other = run.fake.issues[2], "tests/smoke/deployed/test_other.py"
        run.fake.before_read = lambda: issue.update(body=issue["body"].replace(SMOKE, other))
        run.run()
        assert issue["state"] == "open" and run.issue(2).obligations
        assert any(other in line and line.lstrip().startswith("- [ ]") for line in issue["body"].splitlines())


def test_the_verifier_s_record_of_an_item_s_agent_part_stays(monkeypatch):
    build = L.agents.FakeAgents.build

    def with_a_command_for_d2(self, *args, **kwargs):
        result = build(self, *args, **kwargs)
        result.commands.append(dataclasses.replace(result.commands[0], criterion="D2"))
        return result
    monkeypatch.setattr(L.agents.FakeAgents, "build", with_a_command_for_d2)
    with delivered() as run:
        def kinds():
            return sorted(e["kind"] for e in run.issue(2).evidence if e["criterion"] == "D2")
        assert kinds() == ["command"]
        [merge] = run.fake.commits[2]
        run.fake.set_status(merge, RHI, "success", VERIFIED)
        run.run()
        assert kinds() == sorted(["command", L.engine.COMMIT_STATUS_EVIDENCE])
        [comment] = [c["body"] for c in run.fake.comments[present(run.issue(2).pr)] if L.report.EVIDENCE_MARKER in c["body"]]
        assert len([line for line in comment.splitlines() if line.startswith("| D2 ")]) == 2


@pytest.mark.parametrize("path", [SMOKE, "tests/smoke/deployed/test_caf\u00e9.py"], ids=["ascii", "non-ascii"])
def test_a_pull_request_reusing_the_branch_hides_nothing_nor_does_git_s_quoting(path):
    with delivered(item=SIGNS_IN.replace(SMOKE, path), files=(path,)) as run:
        run.fake.create_pr(run.issue(2).branch, "main", "Later work", "")   # the branch is used again
        [merge] = run.fake.commits[2]
        run.fake.set_status(merge, RHI, "success", VERIFIED)
        run.run()
        assert run.issue(2).phase == Phase.DONE and run.fake.issues[2]["state"] == "closed"


def test_each_context_not_verified_on_the_merge_commit_is_read_on_main():
    other = "verify/Other"
    with delivered(files=(), contexts=(RHI, other)) as run:
        [merge] = run.fake.commits[2]
        head = push_main(run, files=(SMOKE,))
        run.fake.set_status(merge, RHI, "success", VERIFIED)   # on a merge commit without the test
        run.fake.set_status(head, other, "success", VERIFIED)
        run.run()
        [record] = [e for e in run.issue(2).evidence if e["criterion"] == "D2"]
        assert (record["head_sha"], record["context"]) == (head, other)
        assert run.fake.issues[2]["state"] == "closed"


def test_an_unconfirmed_close_leaves_the_issue_open_until_github_closes_it():
    with delivered() as run:
        [merge] = run.fake.commits[2]
        run.fake.set_status(merge, RHI, "success", VERIFIED)
        edit = run.fake.edit_issue

        def no_close(n, **fields):
            if fields.get("state") == "closed":
                raise L.github.GitHubError("HTTP 502")
            return edit(n, **fields)
        run.fake.edit_issue = no_close
        run.run()
        assert run.fake.issues[2]["state"] == "open" and run.issue(2).phase not in (Phase.DONE, Phase.DELIVERED)
        assert not run.issue(2).obligations, "discharged all the same"
        done = L.report.render_progress(dataclasses.replace(run.issue(2), phase=Phase.DONE), EPIC).splitlines()[1]
        assert done not in run.comment(2, L.report.PROGRESS_MARKER).splitlines(), "the open issue does not say done"
        run.fake.edit_issue = edit
        run.run()
        assert (run.issue(2).phase, run.fake.issues[2]["state"]) == (Phase.DONE, "closed")


@pytest.mark.parametrize("reread", ["answered", "unanswered"])
def test_a_close_github_applied_without_its_reply_ends_done(reread):
    with delivered() as run:
        [merge] = run.fake.commits[2]
        run.fake.set_status(merge, RHI, "success", VERIFIED)
        edit, get = run.fake.edit_issue, run.fake.get_issue

        def reply_lost(n, **fields):
            answer = edit(n, **fields)
            if fields.get("state") == "closed":
                if reread == "unanswered":   # nor is the issue read again in this run
                    run.fake.get_issue = lambda m: (_ for _ in ()).throw(L.github.GitHubError("HTTP 502"))
                raise L.github.GitHubUnavailable("the reply was lost")
            return answer
        run.fake.edit_issue = reply_lost
        run.run()
        run.fake.edit_issue, run.fake.get_issue = edit, get
        assert run.fake.issues[2]["state"] == "closed"
        if reread == "unanswered":   # the next run, whose work the closed issue no longer is, settles it
            run.run()
        assert run.issue(2).phase == Phase.DONE


def test_a_discharge_keeps_the_rows_another_root_s_run_published():
    later = "tests/smoke/deployed/test_later.py"
    owner = ((SIGNS_IN, DEPLOYED), (SIGNS_IN.replace(SMOKE, later), DEPLOYED))
    run = Loop(contexts=(), verified=VERIFIED, lacks=(DEPLOYED,), unverified=())
    run.deploy = dataclasses.replace(run.deploy, discharge_contexts=(RHI,))
    try:
        run.add_work(2, owner=owner, files=(SMOKE,))
        run.run(root=EPIC)
        run.fake.set_status(push_main(run), RHI, "success", VERIFIED)
        run.run(root=2)   # a run of the issue alone adopts the delivery and proves D2
        pr = present(run.issue(2, root=2).pr)

        def rows():
            return {line.split("|")[1].strip() for line in run.comment(pr, L.report.EVIDENCE_MARKER).splitlines()
                    if line.startswith("| D")}
        assert "D2" in rows()
        run.fake.set_status(push_main(run, files=(later,), removed=(SMOKE,)), RHI, "success", VERIFIED)
        run.run(root=EPIC)   # the epic's run, which never saw D2 proven, proves D3
        assert {"D2", "D3"} <= rows()
        assert run.fake.issues[2]["state"] == "closed"
    finally:
        run.repo.cleanup()


@pytest.mark.parametrize("then", ["main-stays", "main-moves"])
def test_a_tick_github_applied_without_its_reply_is_recorded_before_the_issue_closes(then):
    with delivered() as run:
        verified = push_main(run)   # proven on a later main alone
        run.fake.set_status(verified, RHI, "success", VERIFIED)
        edit = run.fake.edit_issue

        def reply_lost(n, **fields):
            answer = edit(n, **fields)
            if "body" in fields:
                raise L.github.GitHubUnavailable("the reply was lost")
            return answer
        run.fake.edit_issue = reply_lost
        run.run()
        assert "- [x]" in run.fake.issues[2]["body"] and run.fake.issues[2]["state"] == "open"
        run.fake.edit_issue = edit
        if then == "main-moves":   # the proof is no longer on the head the next run reads
            push_main(run, files=("docs/change.md",))
        run.run()
        st = run.issue(2)
        assert (st.phase, run.fake.issues[2]["state"], st.obligations) == (Phase.DONE, "closed", [])
        assert [e["head_sha"] for e in st.evidence if e["criterion"] == "D2"] == [verified]
        assert run.events("obligations_discharged", 2)


@pytest.mark.parametrize("held", ["the-comment", "the-body"])
def test_runs_of_two_roots_discharging_at_once_keep_each_ticked_item_s_row(held):
    later = "tests/smoke/deployed/test_later.py"
    owner = ((SIGNS_IN, DEPLOYED), (SIGNS_IN.replace(SMOKE, later), DEPLOYED))
    run = Loop(contexts=(), verified=VERIFIED, lacks=(DEPLOYED,), unverified=())
    run.deploy = dataclasses.replace(run.deploy, discharge_contexts=(RHI,))
    try:
        run.add_work(2, owner=owner, files=(SMOKE,))
        run.run(root=EPIC)
        run.run(root=2)
        run.fake.set_status(push_main(run), RHI, "success", VERIFIED)   # proves D2 alone
        update, edit, writing, resume = run.fake.update_comment, run.fake.edit_issue, Event(), Event()

        def hold():   # the epic's run writes D2's row, or its tick, once the issue's run has done all for D3
            if not writing.is_set():
                writing.set()
                resume.wait(10)

        def held_comment(number, body):
            if L.report.EVIDENCE_MARKER in body:
                hold()
            return update(number, body)

        def held_body(n, **fields):
            if "body" in fields:
                hold()
            return edit(n, **fields)
        if held == "the-comment":
            run.fake.update_comment = held_comment
        else:
            run.fake.edit_issue = held_body
        with ThreadPoolExecutor() as pool:
            epic = pool.submit(run.run, root=EPIC)
            assert writing.wait(10)
            run.fake.set_status(push_main(run, files=(later,), removed=(SMOKE,)), RHI, "success", VERIFIED)
            try:
                run.run(root=2)   # proves D3
            finally:
                resume.set()
            epic.result()
        pr = present(run.issue(2).pr)
        comment = next(c for c in run.fake.comments[pr] if L.report.EVIDENCE_MARKER in c["body"])
        rows = {line.split("|")[1].strip() for line in comment["body"].splitlines() if line.startswith("| D")}
        assert {"D2", "D3"} <= rows
        body, link = run.fake.issues[2]["body"], f"([evidence]({comment['html_url']}))"
        assert all(f"- [x] {text} {link}" in body for text, _ in owner) and run.fake.issues[2]["state"] == "closed"
    finally:
        run.repo.cleanup()
