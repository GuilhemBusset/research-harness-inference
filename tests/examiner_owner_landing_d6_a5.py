"""Independent assertions over the real engine with fake agents and a local git remote."""
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop/tests"))
support = import_module("test_owner_landing")
BRANCH, DEPLOY, ISSUE = support.BRANCH, support.DEPLOY, support.ISSUE
L, OWNER_LANDING, Phase, WORKFLOW = support.L, support.OWNER_LANDING, support.Phase, support.WORKFLOW
loop, git = support.loop, import_module("helpers").git


def test_multiple_clean_moves_preserve_certificate():
    with loop({WORKFLOW: "jobs: examiner"}) as run:
        run.run()
        head = run.issue().certified_sha
        counts = (len(run.events("verified")), len(run.agent_calls("examine")))
        for index in range(2):
            if index:
                run.owner_pushes(run.handed_over()[0])
                run.run()
            run.main_moves({f"docs/clean-{index}.txt": "unrelated\n"})
            run.run()
            st = run.issue()
            assert st.certified_sha == head and st.owner_landing["head"] == head
            assert git("rev-parse", "HEAD", cwd=Path(st.worktree)) == head
            assert not git("status", "--porcelain", cwd=Path(st.worktree))
            assert (len(run.events("verified")), len(run.agent_calls("examine"))) == counts
            assert len(run.handed_over()) == 1 and run.waits()[1] == OWNER_LANDING
        run.owner_merges(run.pulls()[0])
        run.run()
        assert run.issue().phase == Phase.DONE and not run.calls("merge_pr")
