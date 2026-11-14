"""Independent assertions over the real engine with fake agents and a local git remote."""
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop/tests"))
support = import_module("test_owner_landing")
BRANCH, DEPLOY, ISSUE = support.BRANCH, support.DEPLOY, support.ISSUE
L, OWNER_LANDING, Phase, WORKFLOW = support.L, support.OWNER_LANDING, support.Phase, support.WORKFLOW
loop, git = support.loop, import_module("helpers").git


def test_nonworkflow_github_change_still_lands():
    with loop({".github/examiner.txt": "ordinary"}) as run:
        run.run()
        st = run.issue()
        assert st.phase == Phase.DONE and st.owner_landing is None
        assert run.remote()[BRANCH] == st.certified_sha
        assert len(run.calls("create_pr")) == len(run.calls("merge_pr")) == 1
        assert not run.handed_over() and run.no_fault()
