"""Independent assertions over the real engine with fake agents and a local git remote."""
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/implementation-loop/tests"))
support = import_module("test_owner_landing")
BRANCH, DEPLOY, ISSUE = support.BRANCH, support.DEPLOY, support.ISSUE
L, OWNER_LANDING, Phase, WORKFLOW = support.L, support.OWNER_LANDING, support.Phase, support.WORKFLOW
loop, git = support.loop, import_module("helpers").git


def test_owner_pr_evidence_and_deployment():
    with loop({WORKFLOW: "jobs: examiner"}, contexts=(DEPLOY,)) as run:
        run.run()
        head = run.issue().certified_sha
        run.owner_pushes(run.handed_over()[0])
        run.run()
        [pr] = run.pulls()
        st = run.issue()
        assert pr["body"] == L.report.render_pr_body(ISSUE, "Issue 2", st, "claude", "codex")
        assert any(c["body"] == L.report.render_evidence(ISSUE, st.evidence, head, st.verdicts)
                   for c in run.fake.comments[pr["number"]])
        run.run()
        assert run.waits()[1] == OWNER_LANDING and len(run.calls("create_pr")) == 1
        assert not run.calls("merge_pr") and run.queue_free()
        merge = run.owner_merges(pr, status="pending")
        run.run()
        assert run.issue().phase != Phase.DONE
        run.fake.set_status(merge, DEPLOY, "success")
        run.run()
        st = run.issue()
        assert st.phase == Phase.DONE and st.owner_landing is None
        assert run.events("post_merge_verify")[-1]["ok"]
        assert any(e["kind"] == "deployment" and e["head_sha"] == merge for e in st.evidence)
        assert not run.calls("merge_pr") and run.no_fault()
