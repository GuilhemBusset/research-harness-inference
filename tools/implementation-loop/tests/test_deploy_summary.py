"""What a fix-forward is told of a failed deployment (ADR 0001, decision 5 and amendment 4; system
design, section 8.4; issue #23): `commit_statuses`, and the summary the engine builds from the
statuses of the merge commit whose context is a configured one or lies below one.

The engine runs are real, as in `test_deploying.py`, whose loop and fake pipeline these tests use.
Here the test plays the pipeline's reporter by hand: the first run merges and waits for a verdict,
the test posts the statuses on the merge commit, and the second run reads them."""

import json
import subprocess

from test_deploying import DEPLOY, VERIFY, L, loop  # `L`: the `implement_loop` beside these tests

Phase = L.model.Phase
ISSUE = 2


def fix_forward_feedback(run, statuses, comment=None):
    """Merge the issue, post `statuses` (context, state, description) on its merge commit in that
    order, with `comment` as a commit comment, and run again: the deployment of the fix-forward's
    merge succeeds. Returns the merge commit and what the fix-forward's builder was told."""
    run.run()
    [commit] = run.fake.commits[ISSUE]
    assert run.waits(ISSUE) == (Phase.WAITING, "deploy", "deploying"), "merged, and no verdict yet"
    for context, state, description in statuses:
        run.fake.set_status(commit, context, state, description)
    if comment:
        run.fake.add_commit_comment(commit, comment)
    run.fake.posts[ISSUE] = ["success"]
    run.run()
    assert len(run.events("fix_forward", ISSUE)) == 1 and run.waits(ISSUE)[0] == Phase.DONE
    return commit, run.feedback(ISSUE)[0]


def test_commit_statuses_gives_every_status_with_its_context_state_and_description():
    posted = [   # as GitHub lists them: the newest first, and with more than the three fields
        {"id": 33, "context": f"{DEPLOY}/resource/GatewayService", "state": "failure", "description": "CREATE_FAILED",
         "target_url": None, "creator": {"login": "reporter[bot]"}},
        {"id": 32, "context": DEPLOY, "state": "failure", "description": "failed"},
        {"id": 31, "context": "measure/beta/ttft", "state": "success", "description": None},
        {"id": 30, "context": DEPLOY, "state": "pending", "description": "started"}]
    asked = []

    def gh(args, **kwargs):
        asked.append(args[-1])
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(posted), stderr="")
    fake = L.github.FakeGitHub()
    for status in reversed(posted):
        fake.set_status("abc123", status["context"], status["state"], status["description"] or "")
    expected = [{"context": s["context"], "state": s["state"], "description": s["description"] or ""}
                for s in reversed(posted)]
    assert L.github.GhCli("owner/repo", runner=gh).commit_statuses("abc123") == expected, "every status, the oldest first"
    assert fake.commit_statuses("abc123") == expected, "the fake gives the same"
    assert all(path.startswith("repos/owner/repo/commits/abc123/statuses") for path in asked)
    assert fake.commit_statuses("def456") == [], "another commit's statuses are not this commit's"


def test_the_summary_comes_from_statuses_only():
    with loop(ISSUE, contexts=(DEPLOY, VERIFY), timeout_s=0) as run:
        commit, told = fix_forward_feedback(run, [
            (DEPLOY, "pending", "the deployment started"),
            (f"{VERIFY}/smoke/test_chat_completion", "failure", "the smoke test failed"),
            (VERIFY, "failure", "the verification failed"),
            (f"{DEPLOY}/resource/GatewayService", "failure", "CREATE_FAILED"),
            (DEPLOY, "failure", "the stack was rolled back"),
        ], comment="a commit comment: the bucket of the account could not be reached")

        assert run.fake.commit_comments[commit], "the fake holds the comment"
        assert commit[:12] in told
        assert "GatewayService" in told and "CREATE_FAILED" in told, "the failed resource, with CloudFormation's status code"
        assert "test_chat_completion" in told and "the smoke test failed" in told, "the failed smoke test"
        for context, state, description in ((DEPLOY, "failure", "the stack was rolled back"),
                                            (VERIFY, "failure", "the verification failed")):
            assert f"`{context}` is `{state}`: `{description}`" in told, "a configured context, with its description too"
        assert "the deployment started" not in told, "of a context, the newest status counts"
        assert told.index(f"{DEPLOY}/resource") < told.rindex(f"`{VERIFY}` is") < told.index(f"{VERIFY}/smoke"), \
            "in the configured order, each context before its facts"
        everything_told = json.dumps(run.agents.calls, default=str)
        assert "commit comment" not in everything_told and "bucket" not in everything_told, \
            "nothing of the commit comment reaches an agent"


def test_other_contexts_are_left_out():
    with loop(ISSUE, contexts=(DEPLOY,), timeout_s=0) as run:
        _, told = fix_forward_feedback(run, [
            ("measure/beta/first-token", "success", "412 ms at the median"),
            (f"measure/{DEPLOY}", "failure", "a measurement that failed"),
            (VERIFY, "failure", "the verification failed"),                   # not configured in this run
            (f"{VERIFY}/smoke/test_login", "failure", "the login test failed"),
            (f"{DEPLOY}-canary", "failure", "the canary failed"),             # the same first characters, and no slash
            (f"{DEPLOY}/resource/FleetGroup", "failure", "UPDATE_FAILED"),
            (DEPLOY, "failure", "failed"),
        ])

        assert "FleetGroup" in told and "UPDATE_FAILED" in told, "a fact below the configured context is in"
        for left_out in ("measure/", "412 ms", "a measurement that failed", VERIFY, "the verification failed",
                         "test_login", "the login test failed", f"{DEPLOY}-canary", "the canary failed"):
            assert left_out not in told, f"{left_out!r} is not of a configured context"


def test_statuses_that_cannot_be_read_spend_no_fix_forward():
    with loop(ISSUE) as run:
        run.fake.posts[ISSUE] = ["failure", "success"]

        def refused(sha):
            raise L.github.GitHubError("gh api GET the statuses failed: HTTP 403")
        run.fake.commit_statuses = refused
        run.run()
        [commit] = run.fake.commits[ISSUE]
        assert run.waits(ISSUE) == (Phase.WAITING, "github", "deploying") and run.awaited() == [commit]
        assert not run.events("fix_forward", ISSUE) and not run.events("deployment_failed", ISSUE)

        del run.fake.commit_statuses
        run.run()
        assert len(run.events("fix_forward", ISSUE)) == 1 and run.waits(ISSUE)[0] == Phase.DONE
        assert f"`{DEPLOY}` is `failure`" in run.feedback(ISSUE)[0]
