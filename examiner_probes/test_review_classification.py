"""A review-related refusal enters the owner wait on its first attempt."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path("tools/implementation-loop/tests").resolve()))
from test_owner_review import ISSUE, L, loop


@pytest.mark.parametrize("decision", ["REVIEW_REQUIRED", "CHANGES_REQUESTED"])
def test_first_refusal_is_owner_review(decision):
    with loop(decision) as run:
        attempted = []
        merge = run.fake.merge_pr

        def record(n, sha, method="squash"):
            attempted.append((n, sha))
            return merge(n, sha, method)

        run.fake.merge_pr = record
        run.run()
        state = run.store.issue(ISSUE)
        assert state.phase == L.model.Phase.WAITING
        assert state.wait["kind"] == L.model.WAIT_OWNER_REVIEW == "owner_review"
        assert state.wait["resume"] == "land"
        assert attempted == [(state.pr, state.certified_sha)]
        assert state.merge_refusals == 0
        assert "merge_refused" not in run.kinds()
