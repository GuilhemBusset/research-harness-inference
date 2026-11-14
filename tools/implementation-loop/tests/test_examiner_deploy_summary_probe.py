"""Independent examination of issue #23 on the real engine and GitHub clients."""

import json
import subprocess
from urllib.parse import parse_qs, urlsplit

from test_deploying import DEPLOY, VERIFY, L, Phase, loop


def summary_from_merge(run, statuses):
    run.run()
    [sha] = run.fake.commits[2]
    assert run.waits(2) == (Phase.WAITING, "deploy", "deploying")
    run.fake.add_commit_comment(sha, "COMMENT_ONLY_SENTINEL")
    for context, state, description in statuses:
        run.fake.set_status(sha, context, state, description)
    run.fake.posts[2] = ["success"]
    run.run()
    assert run.waits(2)[0] == Phase.DONE
    assert len(run.events("fix_forward", 2)) == 1
    return sha, run.feedback(2)[0]


def test_every_status_is_read_across_pages():
    posted = [
        {"id": i, "context": f"deploy/beta/fact/{i}", "state": "failure",
         "description": f"FACT_{i}", "target_url": "https://example.com"}
        for i in range(103, 0, -1)
    ]
    pages = []

    def runner(args, **kwargs):
        query = parse_qs(urlsplit(args[-1]).query)
        page = int(query["page"][0])
        per_page = int(query["per_page"][0])
        pages.append(page)
        batch = posted[(page - 1) * per_page:page * per_page]
        return subprocess.CompletedProcess(args, 0, json.dumps(batch), "")

    client = L.github.GhCli("owner/repo", runner=runner)
    expected = [
        {key: row[key] for key in ("context", "state", "description")}
        for row in reversed(posted)
    ]
    fake = L.github.FakeGitHub()
    for row in expected:
        fake.set_status("abc123", row["context"], row["state"], row["description"])
    assert client.commit_statuses("abc123") == expected
    assert fake.commit_statuses("abc123") == expected
    assert pages == [1, 2]
    assert fake.commit_statuses("def456") == []


def test_exact_configured_contexts_include_their_states_and_descriptions():
    with loop(2, contexts=(DEPLOY, VERIFY), timeout_s=0) as run:
        _, told = summary_from_merge(run, [
            (DEPLOY, "failure", "DEPLOYMENT_FAILED"),
            (VERIFY, "success", "VERIFICATION_PASSED"),
        ])
        required = [
            f"`{DEPLOY}` is `failure`", "DEPLOYMENT_FAILED",
            f"`{VERIFY}` is `success`", "VERIFICATION_PASSED",
        ]
        missing = [part for part in required if part not in told]
        assert not missing, f"Configured status fields absent from fix-forward feedback: {missing}"


def test_reporter_resource_and_smoke_facts_reach_the_builder_without_comments():
    with loop(2, contexts=(DEPLOY, VERIFY), timeout_s=0) as run:
        sha, told = summary_from_merge(run, [
            (DEPLOY, "failure", "DEPLOYMENT_FAILED"),
            (f"{DEPLOY}/resource/InferenceService", "failure", "UPDATE_FAILED"),
            (f"{VERIFY}/smoke/test_stream", "failure", "SMOKE_FAILED"),
        ])
        assert sha[:12] in told
        assert f"`{DEPLOY}/resource/InferenceService` is `failure`" in told
        assert "UPDATE_FAILED" in told
        assert f"`{VERIFY}/smoke/test_stream` is `failure`" in told
        assert "SMOKE_FAILED" in told
        assert run.fake.commit_comments[sha] == ["COMMENT_ONLY_SENTINEL"]
        assert "COMMENT_ONLY_SENTINEL" not in json.dumps(run.agents.calls, default=str)


def test_measurements_unconfigured_contexts_and_other_commits_are_excluded():
    with loop(2, contexts=(DEPLOY,), timeout_s=0) as run:
        run.fake.set_status("def456", f"{DEPLOY}/resource/OtherCommit", "failure", "OTHER_COMMIT")
        _, told = summary_from_merge(run, [
            (DEPLOY, "failure", "DEPLOYMENT_FAILED"),
            (f"{DEPLOY}/resource/InferenceService", "failure", "UPDATE_FAILED"),
            ("measure/beta/latency", "failure", "MEASUREMENT_ONLY"),
            (VERIFY, "failure", "UNCONFIGURED_ROOT"),
            (f"{VERIFY}/smoke/test_login", "failure", "UNCONFIGURED_CHILD"),
            (f"{DEPLOY}-canary/resource/Canary", "failure", "SIBLING_ONLY"),
        ])
        assert "InferenceService" in told and "UPDATE_FAILED" in told
        for token in ("measure/", "MEASUREMENT_ONLY", VERIFY, "UNCONFIGURED_ROOT",
                      "UNCONFIGURED_CHILD", "canary", "SIBLING_ONLY", "OtherCommit", "OTHER_COMMIT"):
            assert token not in told
