import sys
from pathlib import Path

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import Phase, loop


def test_other_work_lands_during_approval_then_original_finishes():
    context = 'deploy/examiner'
    with loop(2, 3, contexts=(context,), timeout_s=0, approval='approval') as run:
        run.after[3] = 2
        run.fake.posts[2] = [('pending', 'approval')]
        run.fake.posts[3] = ['success']
        run.run()
        assert run.waits(2) == (Phase.WAITING, 'owner_approval', 'deploying')
        assert run.issue(3).phase == Phase.DONE
        assert run.seq('waiting', 2, wait='owner_approval') < run.seq('merged', 3)
        commit = run.fake.commits[2][0]
        run.fake.set_status(commit, context, 'success')
        run.run()
        assert run.issue(2).phase == Phase.DONE
        assert len(run.fake.merges(2)) == 1
