"""A refusal without an owed review keeps the existing refusal limit."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path("tools/implementation-loop/tests").resolve()))
from test_owner_review import ISSUE, L, loop


@pytest.mark.parametrize("decision", ["APPROVED", ""])
def test_non_review_refusal_is_counted(decision):
    with loop(decision) as run:
        attempts = []

        def refuse(n, sha, method="squash"):
            attempts.append(sha)
            raise L.github.GitHubError("A merge was refused")

        run.fake.merge_pr = refuse
        run.run()
        state = run.store.issue(ISSUE)
        assert state.phase == L.model.Phase.WAITING
        assert state.wait["kind"] == "github"
        assert state.wait["resume"] == "land"
        assert state.merge_refusals == len(attempts) == 3
        assert run.kinds().count("merge_refused") == 3
        assert "owner_review_owed" not in run.kinds()
