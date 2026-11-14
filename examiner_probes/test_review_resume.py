"""Only an approving decision releases an unchanged head from the review wait."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path("tools/implementation-loop/tests").resolve()))
from test_owner_review import ISSUE, L, loop


@pytest.mark.parametrize("decision", ["REVIEW_REQUIRED", "CHANGES_REQUESTED", ""])
def test_unapproved_head_stays_waiting(decision):
    with loop("REVIEW_REQUIRED") as run:
        run.run()
        events = run.kinds()
        run.pull()["review_decision"] = decision
        run.run()
        state = run.store.issue(ISSUE)
        assert state.phase == L.model.Phase.WAITING, "an unapproved head must keep waiting"
        assert state.wait["kind"] == "owner_review"
        assert run.kinds() == events
        assert not run.merges()


def test_approval_lands_without_another_verification():
    with loop("CHANGES_REQUESTED") as run:
        run.run()
        head = run.store.issue(ISSUE).certified_sha
        verified = run.kinds().count("verified")
        run.pull()["review_decision"] = "APPROVED"
        run.run()
        assert run.store.issue(ISSUE).phase == L.model.Phase.DONE
        assert run.merges() == [("merge_pr", run.pull()["number"], head)]
        assert run.kinds().count("verified") == verified


def test_head_movement_requires_verification():
    with loop("REVIEW_REQUIRED") as run:
        run.run()
        verified = run.kinds().count("verified")
        run.pull()["head"]["sha"] = "e" * 40
        run.pull()["head_sha"] = "e" * 40
        run.run()
        assert run.kinds().count("verified") > verified
        assert not run.merges()
