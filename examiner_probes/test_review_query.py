"""Independent checks of the live and in-memory review decision readers."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path("tools/implementation-loop/tests").resolve()))
from test_deploying import L


def test_graphql_review_decision_reader():
    calls = []
    values = iter(["APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", None])

    def runner(args, **kwargs):
        calls.append(args)
        body = {"data": {"repository": {"pullRequest": {"reviewDecision": next(values)}}}}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    gh = L.github.GhCli("example/project", runner=runner)
    assert [gh.review_decision(23) for _ in range(4)] == [
        "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", ""]
    for args in calls:
        assert args[:2] == ["gh", "api"] and "graphql" in args
        assert "query=" + L.github.REVIEW_DECISION_QUERY in args
        assert "owner=example" in args and "name=project" in args
        assert args[args.index("number=23") - 1] == "-F"
        assert "reviewDecision" in L.github.REVIEW_DECISION_QUERY
    fake = L.github.FakeGitHub()
    pr = fake.create_pr("feat/test", "main", "Test", "Test")
    for value in ("APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED", ""):
        pr["review_decision"] = value
        assert fake.review_decision(pr["number"]) == value
