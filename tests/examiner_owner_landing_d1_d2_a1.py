"""Independent assertions over the real engine with fake agents and a local git remote."""
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop/tests"))
support = import_module("test_owner_landing")
BRANCH, DEPLOY, ISSUE = support.BRANCH, support.DEPLOY, support.ISSUE
L, OWNER_LANDING, Phase, WORKFLOW = support.L, support.OWNER_LANDING, support.Phase, support.WORKFLOW
loop, git = support.loop, import_module("helpers").git


def test_handoff_and_absent_remote():
    with loop({WORKFLOW: "jobs: examiner"}) as run:
        run.run()
        st = run.issue()
        head = st.certified_sha
        assert st.owner_landing["branch"] == BRANCH
        assert st.owner_landing["head"] == head
        assert run.waits()[:3] == (Phase.WAITING, OWNER_LANDING, Phase.LAND.value)
        assert set(run.remote()) == {"main"} and run.queue_free() and run.no_fault()
        [comment] = run.handed_over()
        assert BRANCH in comment and head in comment
        assert f"git push origin {head}:refs/heads/{BRANCH}" in comment
        assert "opens the pull request" in comment and "you merge it" in comment
        assert "Do not bring the branch up to date on GitHub" in comment
        for _ in range(2):
            run.run()
            assert run.waits()[1] == OWNER_LANDING and "no branch" in run.waits()[3]
        assert len(run.handed_over()) == 1
        assert not run.calls("create_pr") and not run.calls("merge_pr") and run.no_fault()
