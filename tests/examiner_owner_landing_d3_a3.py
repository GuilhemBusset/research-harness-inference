"""Independent assertions over the real engine with fake agents and a local git remote."""
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop/tests"))
support = import_module("test_owner_landing")
BRANCH, DEPLOY, ISSUE = support.BRANCH, support.DEPLOY, support.ISSUE
L, OWNER_LANDING, Phase, WORKFLOW = support.L, support.OWNER_LANDING, support.Phase, support.WORKFLOW
loop, git = support.loop, import_module("helpers").git


def test_remote_mismatch_then_exact_head():
    with loop({WORKFLOW: "jobs: examiner"}) as run:
        run.run()
        certified = run.issue().certified_sha
        other = git("rev-parse", certified + "^", cwd=run.repo.work)
        git("push", "origin", f"{other}:refs/heads/{BRANCH}", cwd=run.repo.work)
        run.run()
        assert run.waits()[1] == OWNER_LANDING
        assert other[:12] in run.waits()[3] and certified[:12] in run.waits()[3]
        assert not run.calls("create_pr") and run.remote()[BRANCH] == other
        run.owner_pushes(run.handed_over()[0])
        run.run()
        assert len(run.calls("create_pr")) == 1 and not run.calls("merge_pr")
