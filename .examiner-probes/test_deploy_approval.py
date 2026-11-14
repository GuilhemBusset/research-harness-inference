import sys
from pathlib import Path

sys.path.insert(0, str(Path('tools/implementation-loop/tests').resolve()))
from test_deploying import Phase, loop


def test_approval_is_unbounded_and_failure_has_precedence():
    deploy, verify = 'deploy/examiner', 'verify/examiner'
    with loop(2, contexts=(deploy, verify), timeout_s=0, approval='approve please') as run:
        run.fake.posts[2] = [{deploy: ('pending', 'approve please')}, 'success']
        for _ in range(2):
            run.run()
            assert run.waits(2) == (Phase.WAITING, 'owner_approval', 'deploying')
            assert not run.awaited() and not run.queue_claimed()
        commit = run.fake.commits[2][0]
        run.fake.set_status(commit, verify, 'error')
        run.run()
        assert run.events('deployment_failed', 2)[0]['contexts'] == {verify: 'error'}
        assert run.issue(2).phase == Phase.DONE
