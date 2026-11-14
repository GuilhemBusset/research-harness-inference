"""Waiting neither takes the landing claim nor reruns verification."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path("tools/implementation-loop/tests").resolve()))
from test_owner_review import ISSUE, L, loop


def test_wait_is_idle_and_announced_once():
    with loop("REVIEW_REQUIRED") as run:
        run.run()
        before = run.kinds()
        agent_calls = list(run.agents.calls)
        head = run.store.issue(ISSUE).certified_sha
        claims = L.state.RepoClaims(run.repo.work)
        assert claims.acquire(L.engine.MERGE_QUEUE, "independent-review-probe")
        try:
            run.run()
            assert run.store.issue(ISSUE).wait["kind"] == "owner_review"
            assert run.kinds() == before
            assert run.agents.calls == agent_calls
            comments = run.fake.comments[run.pull()["number"]]
            marker = L.engine.OWNER_REVIEW_MARKER.format(head)
            messages = [c["body"] for c in comments if marker in c["body"]]
            assert len(messages) == 1
            assert "waits for the owner's review" in messages[0]
        finally:
            claims.release(L.engine.MERGE_QUEUE, "independent-review-probe")
