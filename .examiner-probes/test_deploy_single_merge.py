import sys
from pathlib import Path

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import Phase, loop


def test_multiple_resumptions_do_not_merge_twice():
    context = 'deploy/examiner'
    with loop(2, contexts=(context,), timeout_s=0) as run:
        for _ in range(3):
            run.run()
            assert run.waits(2) == (Phase.WAITING, 'deploy', 'deploying')
        commit = run.fake.commits[2][0]
        run.fake.set_status(commit, context, 'success')
        run.run()
        assert run.issue(2).phase == Phase.DONE
        assert len(run.events('merged', 2)) == 1
        assert len(run.fake.prs) == 1 and len(run.fake.merges(2)) == 1
