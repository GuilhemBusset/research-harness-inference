"""Independent assertions over the real engine with fake agents and a local git remote."""
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop/tests"))
support = import_module("test_owner_landing")
BRANCH, DEPLOY, ISSUE = support.BRANCH, support.DEPLOY, support.ISSUE
L, OWNER_LANDING, Phase, WORKFLOW = support.L, support.OWNER_LANDING, support.Phase, support.WORKFLOW
loop, git = support.loop, import_module("helpers").git


def test_conflict_before_owner_push_gets_new_certificate():
    with loop({WORKFLOW: "jobs: examiner", "docs/conflict.txt": "branch"}) as run:
        run.run()
        first = run.issue().certified_sha
        examined = len(run.agent_calls("examine"))
        verified = len(run.events("verified"))
        main = run.main_moves({"docs/conflict.txt": "main\n"})
        run.run()
        st = run.issue()
        second = st.certified_sha
        assert second != first and st.verified_sha == second
        assert st.owner_landing["head"] == second
        assert len(run.agent_calls("examine")) > examined and len(run.events("verified")) > verified
        for ancestor in (first, main):
            assert git("merge-base", ancestor, second, cwd=run.repo.work) == ancestor
        [old, new] = run.handed_over()
        assert first in old and second in new
        assert f"git push origin {second}:refs/heads/{BRANCH}" in new
        assert set(run.remote()) == {"main"} and run.queue_free()
        run.owner_pushes(new)
        run.run()
        run.owner_merges(run.pulls()[0])
        run.run()
        assert run.issue().phase == Phase.DONE and not run.calls("merge_pr") and run.no_fault()
